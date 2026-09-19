from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from nexora_api.auth import Principal, verify_token
from nexora_api.config import Settings
from nexora_api.main import create_app
from nexora_api.workspaces import Permission, Role, authorize


@pytest.fixture(scope="session")
def keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return key, public


@pytest.fixture
def auth_settings(keys):
    return Settings(
        auth_issuer="https://identity.example.test/",
        auth_audience="nexora-api",
        auth_public_key=keys[1],
    )


def token(keys, subject="alice", **overrides):
    now = datetime.now(UTC)
    claims = {
        "iss": "https://identity.example.test/",
        "aud": "nexora-api",
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, keys[0], algorithm="RS256")


def test_verified_identity(keys, auth_settings):
    assert verify_token(token(keys), auth_settings) == Principal(auth_settings.auth_issuer, "alice")


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://attacker.test"},
        {"aud": "another-api"},
        {"sub": ""},
        {"sub": " "},
        {"sub": 123},
        {"sub": "x" * 256},
        {"exp": 1},
        {"nbf": 9999999999},
        {"iat": 9999999999},
    ],
)
def test_rejects_invalid_claims(keys, auth_settings, overrides):
    with pytest.raises(HTTPException) as error:
        verify_token(token(keys, **overrides), auth_settings)
    assert error.value.status_code == 401


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud", "sub"])
def test_requires_claims(keys, auth_settings, claim):
    claims = jwt.decode(token(keys), options={"verify_signature": False})
    del claims[claim]
    encoded = jwt.encode(claims, keys[0], algorithm="RS256")
    with pytest.raises(HTTPException) as error:
        verify_token(encoded, auth_settings)
    assert error.value.status_code == 401


def test_wrong_signature_and_algorithm(keys, auth_settings):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(token(keys), options={"verify_signature": False})
    for encoded in [
        jwt.encode(claims, other, algorithm="RS256"),
        jwt.encode(claims, "test-secret" * 4, algorithm="HS256"),
        jwt.encode(claims, None, algorithm="none"),
        "malformed",
        "x" * 8193,
    ]:
        with pytest.raises(HTTPException) as error:
            verify_token(encoded, auth_settings)
        assert error.value.status_code == 401


def test_unconfigured_auth_fails_closed(keys):
    with TestClient(
        create_app(settings=Settings(auth_issuer=None, auth_audience=None, auth_public_key=None))
    ) as client:
        assert (
            client.get("/api/v1/me", headers={"Authorization": "Bearer " + token(keys)}).status_code
            == 503
        )


@pytest.mark.parametrize(
    "method,path",
    [("get", "/api/v1/me"), ("get", "/api/v1/workspaces"), ("post", "/api/v1/workspaces")],
)
def test_no_credentials_rejected_before_database(method, path):
    with TestClient(create_app()) as client:
        response = getattr(client, method)(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


def test_verified_me_and_claims_do_not_grant_roles(keys, auth_settings):
    with TestClient(create_app(settings=auth_settings)) as client:
        response = client.get(
            "/api/v1/me", headers={"Authorization": "Bearer " + token(keys, role="owner")}
        )
        assert response.json() == {"issuer": auth_settings.auth_issuer, "subject": "alice"}


@pytest.mark.parametrize(
    "role,permission,allowed",
    [
        (
            role,
            permission,
            role == Role.OWNER
            or permission == Permission.READ
            or (role == Role.ADMIN and permission == Permission.UPDATE),
        )
        for role in Role
        for permission in Permission
    ],
)
def test_role_matrix(role, permission, allowed):
    if allowed:
        authorize(role, permission)
    else:
        with pytest.raises(HTTPException) as error:
            authorize(role, permission)
        assert error.value.status_code == 403


def test_unknown_roles_denied_and_nonmembers_hidden():
    for role, status in [(None, 404), ("superadmin", 403)]:
        with pytest.raises(HTTPException) as error:
            authorize(role, Permission.READ)
        assert error.value.status_code == status


def test_invalid_deployment_key_is_service_unavailable(keys, auth_settings):
    auth_settings.auth_public_key = "invalid-key"
    with pytest.raises(HTTPException) as error:
        verify_token(token(keys), auth_settings)
    assert error.value.status_code == 503
