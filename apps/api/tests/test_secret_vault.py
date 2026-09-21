import base64
import json
from uuid import uuid4

import pytest

from nexora_api.config import Settings
from nexora_api.secret_vault import (
    KEY_BYTES,
    SecretUnreadable,
    SecretVault,
    VaultKeyUnknown,
    VaultUnavailable,
    build_vault,
    credential_aad,
    mask,
    parse_master_keys,
)

SECRET = "IGQVJ-long-lived-instagram-token-A93X"


def vault(keys, active):
    return build_vault(json.dumps(keys), active)


def test_unconfigured_vault_refuses_to_handle_secrets():
    empty = build_vault(None, None)
    assert empty.configured is False
    with pytest.raises(VaultUnavailable):
        empty.seal(SECRET, credential_aad(uuid4(), uuid4(), "credential"))


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        "[]",
        "{}",
        json.dumps({"k": "not base64!"}),
        # A key of the wrong length silently weakens every ciphertext, so it is refused.
        json.dumps({"k": base64.b64encode(b"short").decode()}),
    ],
)
def test_master_key_material_fails_closed(document):
    with pytest.raises(VaultUnavailable):
        parse_master_keys(document)


def test_active_key_must_be_present(vault_keys):
    with pytest.raises(VaultUnavailable):
        build_vault(json.dumps(vault_keys), "a-key-that-was-never-configured")
    with pytest.raises(VaultUnavailable):
        build_vault(json.dumps(vault_keys), None)


def test_round_trip_keeps_the_secret_and_reveals_only_a_hint(vault_keys):
    store = vault(vault_keys, "test-key-1")
    aad = credential_aad(uuid4(), uuid4(), "credential")
    sealed = store.seal(SECRET, aad)

    assert store.open(sealed, aad) == SECRET
    # Nothing persisted may contain the plaintext.
    assert SECRET not in sealed.ciphertext.decode("latin-1")
    assert SECRET not in sealed.hint
    assert sealed.hint.endswith("A93X")
    assert len(sealed.wrapped_key) >= KEY_BYTES


def test_each_secret_gets_its_own_data_key(vault_keys):
    store = vault(vault_keys, "test-key-1")
    aad = credential_aad(uuid4(), uuid4(), "credential")
    first, second = store.seal(SECRET, aad), store.seal(SECRET, aad)
    assert first.ciphertext != second.ciphertext
    assert first.wrapped_key != second.wrapped_key
    assert first.nonce != second.nonce


def test_associated_data_binds_a_credential_to_one_tenant(vault_keys):
    store = vault(vault_keys, "test-key-1")
    workspace, other, integration = uuid4(), uuid4(), uuid4()
    sealed = store.seal(SECRET, credential_aad(workspace, integration, "credential"))

    # The exact ciphertext, read as another tenant, authenticates rather than decrypts.
    with pytest.raises(SecretUnreadable):
        store.open(sealed, credential_aad(other, integration, "credential"))
    with pytest.raises(SecretUnreadable):
        store.open(sealed, credential_aad(workspace, uuid4(), "credential"))
    with pytest.raises(SecretUnreadable):
        store.open(sealed, credential_aad(workspace, integration, "oauth_verifier"))


def test_tampered_ciphertext_is_rejected(vault_keys):
    store = vault(vault_keys, "test-key-1")
    aad = credential_aad(uuid4(), uuid4(), "credential")
    sealed = store.seal(SECRET, aad)
    flipped = bytes([sealed.ciphertext[0] ^ 0x01]) + sealed.ciphertext[1:]
    with pytest.raises(SecretUnreadable):
        store.open(
            type(sealed)(
                sealed.key_id,
                sealed.wrapped_key,
                sealed.wrap_nonce,
                sealed.nonce,
                flipped,
                sealed.hint,
            ),
            aad,
        )


def test_rotation_reads_the_old_key_and_writes_the_new_one(vault_keys):
    aad = credential_aad(uuid4(), uuid4(), "credential")
    before = vault(vault_keys, "test-key-1")
    sealed = before.seal(SECRET, aad)
    assert sealed.key_id == "test-key-1"

    # Both keys stay loaded during a rotation, so existing credentials keep working.
    after = vault(vault_keys, "test-key-2")
    assert after.open(sealed, aad) == SECRET
    rotated = after.reseal(sealed, aad)
    assert rotated.key_id == "test-key-2"
    assert after.open(rotated, aad) == SECRET

    # Once the old key is withdrawn, ciphertext still sealed under it is unreadable
    # rather than silently returning something wrong.
    without_old = vault({"test-key-2": vault_keys["test-key-2"]}, "test-key-2")
    with pytest.raises(VaultKeyUnknown):
        without_old.open(sealed, aad)
    assert without_old.open(rotated, aad) == SECRET


@pytest.mark.parametrize(
    ("secret", "expected_tail"),
    [
        ("IGQVJ-long-lived-token-A93X", "A93X"),
        ("123456789012", "9012"),
        # Short values give nothing away: four of eight characters is too much.
        ("12345678", ""),
        ("abc", ""),
    ],
)
def test_masking_never_reveals_a_short_secret(secret, expected_tail):
    hint = mask(secret)
    assert hint.startswith("•" * 8)
    assert hint.endswith(expected_tail)
    assert secret not in hint or expected_tail == ""


def test_settings_reject_unusable_vault_configuration(vault_keys):
    with pytest.raises(ValueError):
        Settings(secret_vault_keys=json.dumps({"k": "not-base64"}))
    with pytest.raises(ValueError):
        Settings(secret_vault_active_key="an active key identifier with spaces")
    # Keys without an active identifier are a half-configured vault, not a working one.
    with pytest.raises(VaultUnavailable):
        Settings(secret_vault_keys=json.dumps(vault_keys)).build_secret_vault()


def test_configured_vault_is_usable_from_settings(vault_keys):
    settings = Settings(
        secret_vault_keys=json.dumps(vault_keys), secret_vault_active_key="test-key-1"
    )
    store: SecretVault = settings.build_secret_vault()
    aad = credential_aad(uuid4(), uuid4(), "credential")
    assert store.open(store.seal(SECRET, aad), aad) == SECRET
