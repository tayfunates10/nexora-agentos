"""Envelope encryption for tenant integration credentials.

A stored credential is never readable from the database alone. Each secret gets a fresh
256-bit data key, the secret is sealed with AES-256-GCM under that data key, and the data
key is itself sealed under an operator-held master key that lives outside PostgreSQL.

Two properties matter more than the algorithm choice:

* Associated data binds every ciphertext to the workspace, the integration and the
  purpose it was sealed for. Moving a row between tenants does not produce a decryptable
  secret; it produces an authentication failure.
* The master key is reached through a provider interface. The bundled provider reads keys
  the operator supplied to the process, which is what a development or single-cluster
  deployment needs; a KMS-backed provider implements the same two methods and changes
  nothing above this module.

The vault fails closed. Without configured keys the process starts, but every attempt to
store or read a credential raises VaultUnavailable, so an unconfigured deployment cannot
silently keep secrets in the clear.
"""

import base64
import binascii
import json
import os
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
MAX_SECRET_BYTES = 32_768
KEY_ID_MAX = 64
HINT_VISIBLE = 4


class VaultError(RuntimeError):
    """Base class for vault failures. Never carries secret material in its message."""


class VaultUnavailable(VaultError):
    """The deployment has no usable master key, so secrets cannot be handled at all."""


class VaultKeyUnknown(VaultError):
    """A stored credential references a master key this process was not given."""


class SecretUnreadable(VaultError):
    """Authentication failed: wrong key, wrong tenant binding, or tampered ciphertext."""


@dataclass(frozen=True)
class SealedSecret:
    """The complete ciphertext record. Every field is safe to persist."""

    key_id: str
    wrapped_key: bytes
    wrap_nonce: bytes
    nonce: bytes
    ciphertext: bytes
    hint: str


class MasterKeyProvider(Protocol):
    """The seam a cloud KMS or HashiCorp Vault implementation fills in."""

    @property
    def active_key_id(self) -> str: ...

    def wrap(self, data_key: bytes, aad: bytes) -> tuple[str, bytes, bytes]:
        """Seal a data key. Returns (key_id, nonce, wrapped)."""

    def unwrap(self, key_id: str, wrapped: bytes, nonce: bytes, aad: bytes) -> bytes:
        """Open a data key sealed earlier under key_id."""


class LocalMasterKeyProvider:
    """Master keys held by the process, supplied by the operator's secret store.

    Several keys may be present at once so a rotation can read credentials sealed under
    the previous key while new writes already use the current one.
    """

    def __init__(self, keys: dict[str, bytes], active_key_id: str):
        if not keys:
            raise VaultUnavailable("No master keys configured")
        if active_key_id not in keys:
            raise VaultUnavailable("Active master key is not among the configured keys")
        for key_id, material in keys.items():
            if len(material) != KEY_BYTES:
                raise VaultUnavailable(f"Master key {key_id} must be {KEY_BYTES} bytes")
        self._keys = dict(keys)
        self._active = active_key_id

    @property
    def active_key_id(self) -> str:
        return self._active

    def wrap(self, data_key: bytes, aad: bytes) -> tuple[str, bytes, bytes]:
        nonce = os.urandom(NONCE_BYTES)
        wrapped = AESGCM(self._keys[self._active]).encrypt(nonce, data_key, aad)
        return self._active, nonce, wrapped

    def unwrap(self, key_id: str, wrapped: bytes, nonce: bytes, aad: bytes) -> bytes:
        material = self._keys.get(key_id)
        if material is None:
            raise VaultKeyUnknown("Master key for this credential is not configured")
        try:
            return AESGCM(material).decrypt(nonce, wrapped, aad)
        except InvalidTag:
            raise SecretUnreadable("Credential could not be authenticated") from None


def parse_master_keys(document: str) -> dict[str, bytes]:
    """Read a JSON object mapping key identifier to a base64 256-bit key."""
    try:
        parsed = json.loads(document)
    except json.JSONDecodeError:
        raise VaultUnavailable("Master key material must be a JSON object") from None
    if not isinstance(parsed, dict) or not parsed:
        raise VaultUnavailable("Master key material must be a non-empty JSON object")
    keys: dict[str, bytes] = {}
    for key_id, encoded in parsed.items():
        if not isinstance(key_id, str) or not key_id or len(key_id) > KEY_ID_MAX:
            raise VaultUnavailable("Master key identifiers must be short non-empty strings")
        if not isinstance(encoded, str):
            raise VaultUnavailable(f"Master key {key_id} must be base64 text")
        try:
            material = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise VaultUnavailable(f"Master key {key_id} is not valid base64") from None
        if len(material) != KEY_BYTES:
            raise VaultUnavailable(f"Master key {key_id} must decode to {KEY_BYTES} bytes")
        keys[key_id] = material
    return keys


def credential_aad(workspace_id, integration_id, purpose: str) -> bytes:
    """Bind a ciphertext to one tenant, one integration and one purpose.

    The same bytes must be supplied to open the secret, so a row copied into another
    workspace or reused for another purpose fails authentication instead of decrypting.
    """
    if not purpose or len(purpose) > 64:
        raise VaultError("Credential purpose must be a short label")
    return f"nexora:v1:{workspace_id}:{integration_id}:{purpose}".encode()


def mask(secret: str) -> str:
    """The only representation of a secret that may leave the platform.

    Long secrets keep their last four characters so an operator can tell two credentials
    apart; anything short enough for those characters to matter is masked completely.
    """
    visible = secret[-HINT_VISIBLE:] if len(secret) >= HINT_VISIBLE * 3 else ""
    return "•" * 8 + visible


class SecretVault:
    def __init__(self, provider: MasterKeyProvider | None):
        self._provider = provider

    @property
    def configured(self) -> bool:
        return self._provider is not None

    def _require(self) -> MasterKeyProvider:
        if self._provider is None:
            raise VaultUnavailable("Secret storage is not configured")
        return self._provider

    def seal(self, secret: str, aad: bytes) -> SealedSecret:
        provider = self._require()
        material = secret.encode()
        if not material or len(material) > MAX_SECRET_BYTES:
            raise VaultError("Secret is empty or too large to store")
        data_key = os.urandom(KEY_BYTES)
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(data_key).encrypt(nonce, material, aad)
        key_id, wrap_nonce, wrapped = provider.wrap(data_key, aad)
        return SealedSecret(
            key_id=key_id,
            wrapped_key=wrapped,
            wrap_nonce=wrap_nonce,
            nonce=nonce,
            ciphertext=ciphertext,
            hint=mask(secret),
        )

    def open(self, sealed: SealedSecret, aad: bytes) -> str:
        provider = self._require()
        data_key = provider.unwrap(sealed.key_id, sealed.wrapped_key, sealed.wrap_nonce, aad)
        try:
            return AESGCM(data_key).decrypt(sealed.nonce, sealed.ciphertext, aad).decode()
        except InvalidTag:
            raise SecretUnreadable("Credential could not be authenticated") from None
        except UnicodeDecodeError:
            raise SecretUnreadable("Credential is not valid text") from None

    def reseal(self, sealed: SealedSecret, aad: bytes) -> SealedSecret:
        """Re-encrypt under the current master key, used by key rotation."""
        return self.seal(self.open(sealed, aad), aad)


def build_vault(keys_document: str | None, active_key_id: str | None) -> SecretVault:
    """Construct the vault from operator configuration, or an unconfigured vault."""
    if not keys_document:
        return SecretVault(None)
    keys = parse_master_keys(keys_document)
    if not active_key_id:
        raise VaultUnavailable("An active master key identifier must be configured")
    return SecretVault(LocalMasterKeyProvider(keys, active_key_id))
