"""OIDC discovery and bounded JWKS rotation for API bearer verification."""

import asyncio
import json
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
import jwt

from nexora_api.config import Settings

DISCOVERY_MAX_BYTES = 64 * 1024
JWKS_MAX_BYTES = 256 * 1024
MAX_JWKS_KEYS = 50


class OidcKeyUnavailable(RuntimeError):
    """The configured identity provider cannot supply trustworthy verification keys."""


class OidcKeyNotFound(RuntimeError):
    """The token names no acceptable key in the current provider key set."""


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _require_https_url(url: str, label: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise OidcKeyUnavailable(f"{label} must be an HTTPS URL without credentials or fragment")
    return url


def discovery_url(issuer: str) -> str:
    issuer = _require_https_url(issuer, "OIDC issuer")
    parsed = urlsplit(issuer)
    if parsed.query:
        raise OidcKeyUnavailable("OIDC issuer must not contain a query")
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


class OidcKeyResolver:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self._client = client or httpx.AsyncClient(
            timeout=settings.auth_jwks_timeout_seconds,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._keys: dict[str, object] = {}
        self._expires_at = 0.0
        self._fetched_at = 0.0
        self._discovered_jwks_url: str | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def key_for(self, token: str):
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise OidcKeyNotFound("Malformed token header") from exc
        if header.get("alg") != "RS256":
            raise OidcKeyNotFound("Unsupported token algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise OidcKeyNotFound("Rotating-key tokens require a bounded kid")

        now = self.clock()
        key = self._keys.get(kid)
        if key is not None and now < self._expires_at:
            return key

        async with self._lock:
            now = self.clock()
            key = self._keys.get(kid)
            if key is not None and now < self._expires_at:
                return key

            expired = now >= self._expires_at
            may_force_refresh = (
                now - self._fetched_at >= self.settings.auth_jwks_min_refresh_seconds
            )
            if expired or not self._keys or may_force_refresh:
                await self._refresh(rediscover=expired or not self._discovered_jwks_url)

            key = self._keys.get(kid)
            if key is None:
                raise OidcKeyNotFound("No matching signing key")
            return key

    async def _refresh(self, *, rediscover: bool) -> None:
        jwks_url = self.settings.auth_jwks_url
        if jwks_url:
            jwks_url = _require_https_url(jwks_url, "OIDC JWKS URL")
        elif rediscover or not self._discovered_jwks_url:
            jwks_url = await self._discover_jwks_url()
            self._discovered_jwks_url = jwks_url
        else:
            jwks_url = self._discovered_jwks_url

        payload = await self._get_json(jwks_url, JWKS_MAX_BYTES, "JWKS")
        keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list) or not keys or len(keys) > MAX_JWKS_KEYS:
            raise OidcKeyUnavailable("JWKS must contain a bounded non-empty keys array")

        resolved: dict[str, object] = {}
        for item in keys:
            if not isinstance(item, dict):
                continue
            if "d" in item:
                raise OidcKeyUnavailable("JWKS must never contain private RSA material")
            kid = item.get("kid")
            if (
                item.get("kty") != "RSA"
                or item.get("use") not in (None, "sig")
                or item.get("alg") not in (None, "RS256")
                or not isinstance(kid, str)
                or not kid
                or len(kid) > 256
            ):
                continue
            if kid in resolved:
                raise OidcKeyUnavailable("JWKS contains a duplicate kid")
            try:
                resolved[kid] = jwt.PyJWK.from_dict(item, algorithm="RS256").key
            except (jwt.PyJWKError, jwt.InvalidKeyError, ValueError, TypeError) as exc:
                raise OidcKeyUnavailable("JWKS contains an invalid RSA signing key") from exc

        if not resolved:
            raise OidcKeyUnavailable("JWKS contains no acceptable RS256 signing keys")

        now = self.clock()
        self._keys = resolved
        self._fetched_at = now
        self._expires_at = now + self.settings.auth_jwks_cache_seconds

    async def _discover_jwks_url(self) -> str:
        issuer = self.settings.auth_issuer
        if not issuer:
            raise OidcKeyUnavailable("Authentication issuer is not configured")
        payload = await self._get_json(discovery_url(issuer), DISCOVERY_MAX_BYTES, "OIDC discovery")
        if not isinstance(payload, dict) or payload.get("issuer") != issuer:
            raise OidcKeyUnavailable("OIDC discovery issuer does not match configured issuer")
        jwks_url = payload.get("jwks_uri")
        if not isinstance(jwks_url, str):
            raise OidcKeyUnavailable("OIDC discovery did not provide jwks_uri")
        jwks_url = _require_https_url(jwks_url, "OIDC discovery jwks_uri")
        if _origin(jwks_url) != _origin(issuer):
            raise OidcKeyUnavailable(
                "Cross-origin jwks_uri requires explicit NEXORA_AUTH_JWKS_URL"
            )
        return jwks_url

    async def _get_json(self, url: str, max_bytes: int, label: str) -> dict:
        try:
            async with self._client.stream(
                "GET",
                url,
                headers={"accept": "application/json"},
            ) as response:
                if response.status_code != 200:
                    raise OidcKeyUnavailable(f"{label} returned HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise OidcKeyUnavailable(f"{label} exceeded the response size limit")
        except OidcKeyUnavailable:
            raise
        except httpx.HTTPError as exc:
            raise OidcKeyUnavailable(f"{label} request failed") from exc

        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OidcKeyUnavailable(f"{label} returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise OidcKeyUnavailable(f"{label} must return a JSON object")
        return parsed
