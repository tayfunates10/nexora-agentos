from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nexora_api.config import Settings
from nexora_api.oidc_keys import OidcKeyNotFound, OidcKeyResolver, OidcKeyUnavailable
from nexora_api.rate_limit import RateLimitUnavailable


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str


bearer = HTTPBearer(auto_error=False)


def _decode_token(token: str, key, settings: Settings) -> Principal:
    if len(token) > 8192:
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 255:
            raise jwt.InvalidTokenError("Invalid subject")
        if any(type(claims[field]) not in (int, float) for field in ("exp", "iat")):
            raise jwt.InvalidTokenError("Invalid timestamps")
        if claims["exp"] <= claims["iat"]:
            raise jwt.InvalidTokenError("Invalid lifetime")
        return Principal(issuer=claims["iss"], subject=subject)
    except jwt.InvalidTokenError:
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"}) from None
    except (jwt.InvalidKeyError, ValueError, TypeError):
        raise HTTPException(503, "Authentication configuration invalid") from None


def verify_token(token: str, settings: Settings) -> Principal:
    """Verify against an explicitly pinned PEM; kept for deterministic tests/pinned deployments."""
    if not all([settings.auth_issuer, settings.auth_audience, settings.auth_public_key]):
        raise HTTPException(503, "Authentication is not configured")
    return _decode_token(token, settings.auth_public_key, settings)


async def verify_request_token(
    token: str,
    settings: Settings,
    key_resolver: OidcKeyResolver,
) -> Principal:
    if settings.auth_public_key:
        return verify_token(token, settings)
    if not settings.auth_issuer or not settings.auth_audience:
        raise HTTPException(503, "Authentication is not configured")
    if len(token) > 8192:
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
    try:
        key = await key_resolver.key_for(token)
    except OidcKeyNotFound:
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"}) from None
    except OidcKeyUnavailable:
        raise HTTPException(503, "Authentication key service unavailable") from None
    return _decode_token(token, key, settings)


async def authenticated(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None:
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
    principal = await verify_request_token(
        credentials.credentials,
        request.app.state.settings,
        request.app.state.oidc_keys,
    )
    try:
        decision = await request.app.state.identity_rate_limiter.check(
            principal.issuer, principal.subject
        )
    except RateLimitUnavailable:
        raise HTTPException(503, "Rate limiting unavailable") from None
    if not decision.allowed:
        raise HTTPException(
            429,
            "Rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
    return principal
