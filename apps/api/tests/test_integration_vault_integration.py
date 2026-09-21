import json
import os

import psycopg
import pytest
from catalog_support import connect_api_key, headers, publish_connector, workspace
from conftest import PLATFORM_ADMIN
from fastapi.testclient import TestClient

from nexora_api.main import create_app
from nexora_api.migrate import migrate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL"),
]

SECRET = "mikro-live-service-key-9f3b21ccA93X"
OTHER_SECRET = "second-mikro-service-key-77d1ffB71Z"


@pytest.fixture
def client(platform_settings):
    migrate(platform_settings)
    with TestClient(create_app(settings=platform_settings)) as running:
        yield running


@pytest.fixture
def registry(client, keys):
    admin = headers(keys, PLATFORM_ADMIN)
    for connector in ("mikro", "instagram", "erp"):
        publish_connector(client, admin, connector)
    return admin


def test_the_connector_registry_is_published_not_compiled_in(client, registry, keys):
    """A new service becomes available to every tenant without a platform code change."""
    caller = headers(keys, "owner-registry")
    catalog = client.get("/api/v1/integration-catalog?limit=100", headers=caller)
    assert catalog.status_code == 200, catalog.text
    by_id = {item["id"]: item for item in catalog.json()["items"]}
    assert {"mikro", "instagram", "erp"} <= set(by_id)
    assert by_id["instagram"]["auth_type"] == "oauth2"
    # The tenant-facing view describes the fields to fill in, never a value or a client
    # secret belonging to the operator.
    for item in by_id.values():
        assert "client_secret" not in json.dumps(item)
        for field in item["credential_fields"]:
            assert set(field) <= {"key", "label", "secret", "required", "help", "pattern"}


def test_a_stored_secret_never_comes_back(client, registry, keys):
    owner = headers(keys, "owner-secret")
    space = workspace(client, owner, "Secret handling")
    created = connect_api_key(client, owner, space, "mikro", SECRET)
    assert created.status_code == 201, created.text
    integration = created.json()

    assert integration["status"] == "connected"
    assert integration["credential_hint"].endswith("A93X")
    assert integration["credential_hint"].startswith("•")
    # The non-secret fields a person needs to recognise the connection stay readable.
    assert integration["config"] == {"base_url": "https://erp.example.com"}

    # No response the platform can produce contains the value.
    for response in (
        created,
        client.get(f"/api/v1/workspaces/{space}/integrations", headers=owner),
        client.get(f"/api/v1/workspaces/{space}/integrations/{integration['id']}", headers=owner),
        client.post(
            f"/api/v1/workspaces/{space}/integrations/{integration['id']}/test", headers=owner
        ),
    ):
        assert SECRET not in response.text, response.url


def test_the_database_holds_ciphertext_and_the_secret_is_bound_to_its_tenant(
    client, registry, keys, platform_settings
):
    owner = headers(keys, "owner-ciphertext")
    space = workspace(client, owner, "Ciphertext")
    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()

    with psycopg.connect(platform_settings.database_url.get_secret_value()) as connection:
        row = connection.execute(
            """SELECT key_id,ciphertext,wrapped_key,hint,workspace_id
               FROM integration_credentials WHERE tenant_integration_id=%s""",
            (integration["id"],),
        ).fetchone()
        assert row is not None
        key_id, ciphertext, wrapped_key, hint, workspace_id = row
        assert key_id == "test-key-1"
        assert SECRET.encode() not in bytes(ciphertext)
        assert SECRET.encode() not in bytes(wrapped_key)
        assert SECRET not in hint
        assert str(workspace_id) == space

        # Nothing in the tenant row carries the value either.
        stored = connection.execute(
            "SELECT config::text,scopes::text FROM tenant_integrations WHERE id=%s",
            (integration["id"],),
        ).fetchone()
        assert SECRET not in "".join(stored)

    # The ciphertext is bound to this workspace: read under another tenant it fails to
    # authenticate rather than decrypting.
    from uuid import UUID

    from nexora_api.secret_vault import SealedSecret, SecretUnreadable, credential_aad

    vault = platform_settings.build_secret_vault()
    with psycopg.connect(platform_settings.database_url.get_secret_value()) as connection:
        record = connection.execute(
            """SELECT key_id,wrapped_key,wrap_nonce,nonce,ciphertext,hint
               FROM integration_credentials WHERE tenant_integration_id=%s""",
            (integration["id"],),
        ).fetchone()
    sealed = SealedSecret(
        record[0], bytes(record[1]), bytes(record[2]), bytes(record[3]), bytes(record[4]), record[5]
    )
    own = credential_aad(UUID(space), UUID(integration["id"]), "credential")
    assert json.loads(vault.open(sealed, own))["api_key"] == SECRET
    stolen = credential_aad(UUID(int=7), UUID(integration["id"]), "credential")
    with pytest.raises(SecretUnreadable):
        vault.open(sealed, stolen)


def test_a_connection_survives_signing_out_and_coming_back(
    client, registry, keys, platform_settings
):
    """The acceptance criterion: connect once, return tomorrow, still connected."""
    owner = headers(keys, "owner-persistence")
    space = workspace(client, owner, "Persistence")
    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()

    # A new process with a new token, as if the browser had been closed overnight.
    with TestClient(create_app(settings=platform_settings)) as tomorrow:
        listing = tomorrow.get(
            f"/api/v1/workspaces/{space}/integrations",
            headers=headers(keys, "owner-persistence"),
        )
        assert listing.status_code == 200, listing.text
        restored = listing.json()["items"]
        assert [item["id"] for item in restored] == [integration["id"]]
        assert restored[0]["status"] == "connected"
        assert restored[0]["credential_hint"].endswith("A93X")
        # Nothing was asked of the person again.
        assert SECRET not in listing.text


def test_rotating_a_credential_destroys_the_one_it_replaced(
    client, registry, keys, platform_settings
):
    owner = headers(keys, "owner-rotation")
    space = workspace(client, owner, "Rotation")
    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()

    rotated = client.put(
        f"/api/v1/workspaces/{space}/integrations/{integration['id']}/credential",
        json={"credentials": {"api_key": OTHER_SECRET, "base_url": "https://erp.example.com"}},
        headers=owner,
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["credential_hint"].endswith("B71Z")
    assert OTHER_SECRET not in rotated.text

    with psycopg.connect(platform_settings.database_url.get_secret_value()) as connection:
        rows = connection.execute(
            "SELECT count(*) FROM integration_credentials WHERE tenant_integration_id=%s",
            (integration["id"],),
        ).fetchone()
        # Exactly one ciphertext exists: a rotated secret leaves no second copy behind.
        assert rows[0] == 1


def test_one_workspace_connects_the_same_service_several_times(client, registry, keys):
    """An agency runs one Instagram-shaped connection per customer."""
    owner = headers(keys, "owner-agency")
    space = workspace(client, owner, "Agency")
    accounts = []
    for name in ("Arma Digital", "Customer A", "Customer B"):
        created = connect_api_key(
            client,
            owner,
            space,
            "mikro",
            f"key-for-{name.lower().replace(' ', '-')}-000{len(accounts)}",
            display_name=name,
            account_identifier=name.lower().replace(" ", "-"),
        )
        assert created.status_code == 201, created.text
        accounts.append(created.json())

    assert len({item["id"] for item in accounts}) == 3
    listing = client.get(f"/api/v1/workspaces/{space}/integrations", headers=owner).json()
    assert len(listing["items"]) == 3
    assert {item["display_name"] for item in listing["items"]} == {
        "Arma Digital",
        "Customer A",
        "Customer B",
    }

    # The same account cannot be connected twice by accident.
    duplicate = connect_api_key(
        client,
        owner,
        space,
        "mikro",
        SECRET,
        display_name="Arma Digital again",
        account_identifier="arma-digital",
    )
    assert duplicate.status_code == 409


def test_a_tenant_cannot_see_or_touch_another_tenants_connection(client, registry, keys):
    owner_a, owner_b = headers(keys, "owner-iso-a"), headers(keys, "owner-iso-b")
    space_a, space_b = workspace(client, owner_a, "A"), workspace(client, owner_b, "B")
    integration = connect_api_key(client, owner_a, space_a, "mikro", SECRET).json()

    # Reading another workspace at all is a 404, not a 403: nothing is confirmed.
    assert (
        client.get(f"/api/v1/workspaces/{space_a}/integrations", headers=owner_b).status_code == 404
    )
    assert (
        client.get(
            f"/api/v1/workspaces/{space_a}/integrations/{integration['id']}", headers=owner_b
        ).status_code
        == 404
    )
    # Nor can B reach A's connection by naming it inside B's own workspace.
    assert (
        client.get(
            f"/api/v1/workspaces/{space_b}/integrations/{integration['id']}", headers=owner_b
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/v1/workspaces/{space_b}/integrations/{integration['id']}/credential",
            json={"credentials": {"api_key": "stolen", "base_url": "https://erp.example.com"}},
            headers=owner_b,
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/v1/workspaces/{space_b}/integrations/{integration['id']}", headers=owner_b
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/workspaces/{space_b}/integrations/{integration['id']}/test", headers=owner_b
        ).status_code
        == 404
    )
    # A's connection is untouched.
    assert (
        client.get(
            f"/api/v1/workspaces/{space_a}/integrations/{integration['id']}", headers=owner_a
        ).json()["status"]
        == "connected"
    )


def test_only_a_tenant_administrator_may_connect_a_service(client, registry, keys):
    owner, member = headers(keys, "owner-roles"), headers(keys, "member-roles")
    space = workspace(client, owner, "Roles")
    assert (
        client.put(
            f"/api/v1/workspaces/{space}/members",
            json={"subject": "member-roles", "role": "member"},
            headers=owner,
        ).status_code
        == 200
    )
    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()

    # A normal user sees that a connection exists but cannot create or change one.
    listing = client.get(f"/api/v1/workspaces/{space}/integrations", headers=member)
    assert listing.status_code == 200
    assert listing.json()["items"][0]["credential_hint"].endswith("A93X")
    assert SECRET not in listing.text

    assert connect_api_key(client, member, space, "erp", "another-key").status_code == 403
    assert (
        client.put(
            f"/api/v1/workspaces/{space}/integrations/{integration['id']}/credential",
            json={"credentials": {"api_key": "x", "base_url": "https://erp.example.com"}},
            headers=member,
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/v1/workspaces/{space}/integrations/{integration['id']}", headers=member
        ).status_code
        == 403
    )


def test_credentials_are_checked_against_the_published_connector(client, registry, keys):
    owner = headers(keys, "owner-validation")
    space = workspace(client, owner, "Validation")

    def connect(credentials):
        return client.post(
            f"/api/v1/workspaces/{space}/integrations",
            json={
                "integration_definition_id": "mikro",
                "display_name": "Mikro",
                "account_identifier": "acct",
                "credentials": credentials,
            },
            headers=owner,
        )

    assert connect({"base_url": "https://erp.example.com"}).status_code == 422
    assert connect({"api_key": SECRET}).status_code == 422
    assert (
        connect(
            {"api_key": SECRET, "base_url": "https://erp.example.com", "surprise": "x"}
        ).status_code
        == 422
    )
    # A base URL that would reach inside the platform is refused outright.
    for hostile in ("http://erp.example.com", "https://169.254.169.254", "https://localhost"):
        assert connect({"api_key": SECRET, "base_url": hostile}).status_code == 422
    assert connect({"api_key": SECRET, "base_url": "https://erp.example.com"}).status_code == 201


def test_an_oauth_service_cannot_be_connected_by_pasting_a_token(client, registry, keys):
    owner = headers(keys, "owner-oauth-guard")
    space = workspace(client, owner, "OAuth guard")
    response = client.post(
        f"/api/v1/workspaces/{space}/integrations",
        json={
            "integration_definition_id": "instagram",
            "display_name": "Instagram",
            "account_identifier": "@arcatesyazilim",
            "credentials": {"access_token": "pasted-token"},
        },
        headers=owner,
    )
    assert response.status_code == 409


def test_an_oauth_flow_needs_an_operator_registered_client(client, registry, keys):
    """Without a registered application the platform refuses rather than improvising."""
    owner = headers(keys, "owner-oauth-start")
    space = workspace(client, owner, "OAuth start")
    response = client.post(
        f"/api/v1/workspaces/{space}/integrations/oauth/start",
        json={
            "integration_definition_id": "instagram",
            "display_name": "Instagram",
            "account_identifier": "@arcatesyazilim",
            "scopes": ["instagram_basic"],
        },
        headers=owner,
    )
    assert response.status_code == 503


def test_the_platform_refuses_to_store_a_secret_without_a_vault(keys, vault_keys):
    """An unconfigured deployment fails closed instead of keeping secrets in the clear."""
    from nexora_api.config import Settings

    settings = Settings(
        auth_issuer="https://identity.example.test/",
        auth_audience="nexora-api",
        auth_public_key=keys[1],
        platform_admin_subjects=PLATFORM_ADMIN,
        database_url=os.environ["NEXORA_DATABASE_URL"],
    )
    migrate(settings)
    with TestClient(create_app(settings=settings)) as unconfigured:
        admin = headers(keys, PLATFORM_ADMIN)
        publish_connector(unconfigured, admin, "mikro")
        owner = headers(keys, "owner-no-vault")
        space = workspace(unconfigured, owner, "No vault")
        response = connect_api_key(unconfigured, owner, space, "mikro", SECRET)
        assert response.status_code == 503
        assert SECRET not in response.text


def test_connection_changes_are_audited_without_the_secret(
    client, registry, keys, platform_settings
):
    owner = headers(keys, "owner-audit-vault", request_id="vault-audit")
    space = workspace(client, owner, "Audit")
    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()
    client.put(
        f"/api/v1/workspaces/{space}/integrations/{integration['id']}/credential",
        json={"credentials": {"api_key": OTHER_SECRET, "base_url": "https://erp.example.com"}},
        headers=owner,
    )
    client.patch(
        f"/api/v1/workspaces/{space}/integrations/{integration['id']}",
        json={"enabled": False},
        headers=owner,
    )
    assert (
        client.delete(
            f"/api/v1/workspaces/{space}/integrations/{integration['id']}", headers=owner
        ).status_code
        == 204
    )

    with psycopg.connect(platform_settings.database_url.get_secret_value()) as connection:
        rows = connection.execute(
            "SELECT action FROM security_events WHERE workspace_id=%s ORDER BY created_at",
            (space,),
        ).fetchall()
        actions = [row[0] for row in rows]
        assert "integration.added" in actions
        assert "integration.credential_rotated" in actions
        assert "integration.disabled" in actions
        assert "integration.removed" in actions
        # The audit trail records what happened, never the value it happened to.
        assert not any(SECRET in row[0] or OTHER_SECRET in row[0] for row in rows)
