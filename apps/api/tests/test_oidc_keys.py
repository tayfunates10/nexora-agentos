import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from nexora_api.auth import Principal, verify_request_token
from nexora_api.config import Settings
from nexora_api.oidc_keys import OidcKeyResolver, OidcKeyUnavailable, discovery_url


def run(coro):
    return asyncio.run(coro)


def make_key(kid):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private, jwk


def make_token(private, kid, *, jku=None):
    now = datetime.now(UTC)
    headers = {"kid": kid}
    if jku:
        headers["jku"] = jku
    return jwt.encode(
        {
            "iss": "https://identity.example.test/tenant",
            "aud": "nexora-api",
            "sub": "alice",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private,
        algorithm="RS256",
        headers=headers,
    )


def settings(**updates):
    base = Settings(
        auth_issuer="https://identity.example.test/tenant",
        auth_audience="nexora-api",
        auth_public_key=None,
        auth_jwks_cache_seconds=300,
        auth_jwks_min_refresh_seconds=10,
    )
    return base.model_copy(update=updates)


def test_discovery_url_appends_to_path_issuer():
    assert (
        discovery_url("https://identity.example.test/tenant/")
        == "https://identity.example.test/tenant/.well-known/openid-configuration"
    )


def test_rotates_on_unknown_kid_without_following_token_jku():
    old_private, old_jwk = make_key("old")
    new_private, new_jwk = make_key("new")
    now = [100.0]
    current = {"keys": [old_jwk]}
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test/tenant",
                    "jwks_uri": "https://identity.example.test/tenant/jwks",
                },
            )
        if request.url.path == "/tenant/jwks":
            return httpx.Response(200, json=current)
        return httpx.Response(404)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = OidcKeyResolver(settings(), client=client, clock=lambda: now[0])
            first = await verify_request_token(
                make_token(
                    old_private,
                    "old",
                    jku="https://attacker.example/controlled-jwks",
                ),
                settings(),
                resolver,
            )
            assert first == Principal("https://identity.example.test/tenant", "alice")
            current["keys"] = [new_jwk]
            now[0] += 11
            second = await verify_request_token(
                make_token(new_private, "new"), settings(), resolver
            )
            assert second == first

    run(scenario())
    assert all("attacker.example" not in call for call in calls)
    assert sum(call.endswith("/tenant/jwks") for call in calls) == 2
    assert sum("openid-configuration" in call for call in calls) == 1


def test_cache_avoids_network_fetch_for_known_kid():
    private, jwk = make_key("one")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "openid-configuration" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test/tenant",
                    "jwks_uri": "https://identity.example.test/tenant/jwks",
                },
            )
        return httpx.Response(200, json={"keys": [jwk]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = OidcKeyResolver(settings(), client=client)
            encoded = make_token(private, "one")
            await verify_request_token(encoded, settings(), resolver)
            await verify_request_token(encoded, settings(), resolver)

    run(scenario())
    assert len(calls) == 2


def test_cross_origin_discovery_requires_operator_override():
    private, jwk = make_key("one")

    def handler(request):
        if "openid-configuration" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test/tenant",
                    "jwks_uri": "https://cdn.example.test/jwks",
                },
            )
        return httpx.Response(200, json={"keys": [jwk]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = OidcKeyResolver(settings(), client=client)
            with pytest.raises(OidcKeyUnavailable, match="Cross-origin"):
                await resolver.key_for(make_token(private, "one"))

    run(scenario())


def test_explicit_cross_origin_jwks_url_skips_discovery():
    private, jwk = make_key("one")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"keys": [jwk]})

    async def scenario():
        configured = settings(auth_jwks_url="https://cdn.example.test/jwks")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = OidcKeyResolver(configured, client=client)
            principal = await verify_request_token(make_token(private, "one"), configured, resolver)
            assert principal.subject == "alice"

    run(scenario())
    assert calls == ["https://cdn.example.test/jwks"]


def test_provider_failure_fails_closed_as_service_unavailable():
    private, _ = make_key("one")

    def handler(request):
        return httpx.Response(503)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = OidcKeyResolver(settings(), client=client)
            with pytest.raises(HTTPException) as error:
                await verify_request_token(make_token(private, "one"), settings(), resolver)
            assert error.value.status_code == 503

    run(scenario())


def test_dynamic_tokens_require_rs256_and_kid():
    private, _ = make_key("one")
    now = datetime.now(UTC)
    claims = {
        "iss": "https://identity.example.test/tenant",
        "aud": "nexora-api",
        "sub": "alice",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    invalid_tokens = [
        jwt.encode(claims, private, algorithm="RS256"),
        jwt.encode(
            claims,
            "test-secret" * 4,
            algorithm="HS256",
            headers={"kid": "one"},
        ),
    ]

    async def scenario():
        resolver = OidcKeyResolver(settings())
        try:
            for encoded in invalid_tokens:
                with pytest.raises(HTTPException) as error:
                    await verify_request_token(encoded, settings(), resolver)
                assert error.value.status_code == 401
        finally:
            await resolver.aclose()

    run(scenario())


def test_unknown_kid_forced_refresh_is_throttled():
    private, jwk = make_key("known")
    attacker, _ = make_key("missing")
    now = [100.0]
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "openid-configuration" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test/tenant",
                    "jwks_uri": "https://identity.example.test/tenant/jwks",
                },
            )
        return httpx.Response(200, json={"keys": [jwk]})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = OidcKeyResolver(settings(), client=client, clock=lambda: now[0])
            await verify_request_token(make_token(private, "known"), settings(), resolver)
            baseline = len(calls)

            now[0] += 1
            for kid in ("random-one", "random-two"):
                with pytest.raises(HTTPException) as error:
                    await verify_request_token(make_token(attacker, kid), settings(), resolver)
                assert error.value.status_code == 401
            assert len(calls) == baseline

            now[0] += 10
            with pytest.raises(HTTPException) as error:
                await verify_request_token(
                    make_token(attacker, "random-three"), settings(), resolver
                )
            assert error.value.status_code == 401
            assert len(calls) == baseline + 1

    run(scenario())
