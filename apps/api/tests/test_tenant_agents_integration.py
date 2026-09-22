import asyncio
import json
import os
from types import SimpleNamespace
from uuid import UUID

import psycopg
import pytest
from catalog_support import (
    catalog_entry,
    connect_api_key,
    create_catalog_agent,
    headers,
    publish_connector,
    publish_version,
    unique_slug,
    workspace,
)
from conftest import PLATFORM_ADMIN
from fastapi.testclient import TestClient
from pydantic import SecretStr

from nexora_api.connector_mcp import ConnectorMcpAdapter
from nexora_api.main import create_app
from nexora_api.mcp_gateway import McpAdapterError
from nexora_api.migrate import migrate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL"),
]

SECRET = "tenant-agent-service-key-8821ffA93X"


@pytest.fixture
def client(platform_settings):
    migrate(platform_settings)
    with TestClient(create_app(settings=platform_settings)) as running:
        yield running


@pytest.fixture
def admin(client, keys):
    caller = headers(keys, PLATFORM_ADMIN)
    for connector in ("mikro", "erp", "instagram"):
        publish_connector(client, caller, connector)
    return caller


def standard_agent(client, admin, slug, version="1.0.0", **manifest):
    """A catalog agent whose required integration is one a test can actually connect."""
    create_catalog_agent(client, admin, "social-media", slug)
    manifest.setdefault("required_integrations", ["mikro"])
    manifest.setdefault("optional_integrations", ["erp"])
    manifest.setdefault("required_tools", [])
    manifest.setdefault("optional_tools", [])
    return publish_version(
        client,
        admin,
        "social-media",
        slug,
        version,
        **manifest,
    )


def install(client, caller, space, slug, **body):
    return client.post(
        f"/api/v1/workspaces/{space}/tenant-agents", json={"slug": slug, **body}, headers=caller
    )


def test_an_agent_without_its_required_connection_never_becomes_active(client, admin, keys):
    owner = headers(keys, "owner-readiness")
    space = workspace(client, owner, "Readiness")
    slug = unique_slug("readiness-agent")
    standard_agent(client, admin, slug)

    created = install(client, owner, space, slug)
    assert created.status_code == 201, created.text
    instance = created.json()
    assert instance["status"] == "paused"
    assert instance["readiness"]["ready"] is False
    assert instance["readiness"]["missing_required"] == ["mikro"]
    # The screen can name exactly what to connect, and what is merely optional.
    requirements = {
        item["integration_definition_id"]: item for item in instance["readiness"]["requirements"]
    }
    assert requirements["mikro"]["required"] is True
    assert requirements["mikro"]["satisfied"] is False
    assert requirements["erp"]["required"] is False

    # Activation is refused while the requirement is unmet.
    refused = client.patch(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}",
        json={"status": "active"},
        headers=owner,
    )
    assert refused.status_code == 409

    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()
    bound = client.put(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/bindings/mikro",
        json={"tenant_integration_id": integration["id"]},
        headers=owner,
    )
    assert bound.status_code == 200, bound.text
    # Binding the required connection is what makes the agent runnable.
    assert bound.json()["readiness"]["ready"] is True
    assert bound.json()["status"] == "active"
    assert SECRET not in bound.text


def test_standard_agent_runs_directly_with_an_immutable_execution_snapshot(
    client, admin, keys, platform_settings
):
    owner = headers(keys, "owner-standard-run")
    space = workspace(client, owner, "Standard run")
    slug = unique_slug("standard-run-agent")
    standard_agent(
        client,
        admin,
        slug,
        "1.0.0",
        system_instructions="Version one instructions.",
        model_policy={"primary": "balanced-v1"},
        required_tools=["mikro.stock.read"],
    )
    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()
    instance = install(
        client,
        owner,
        space,
        slug,
        bindings={"mikro": integration["id"]},
    ).json()

    run_headers = {**owner, "Idempotency-Key": "standard-run-0001"}
    created = client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/runs",
        json={"input": "Inspect the current account and summarize what should be done."},
        headers=run_headers,
    )
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["agent_id"] == instance["id"]
    assert run["status"] == "queued"

    replay = client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/runs",
        json={"input": "Inspect the current account and summarize what should be done."},
        headers=run_headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == run["id"]

    with psycopg.connect(platform_settings.database_url.get_secret_value()) as connection:
        stored = connection.execute(
            """SELECT agent_id,tenant_agent_id,agent_snapshot
               FROM agent_runs WHERE id=%s""",
            (run["id"],),
        ).fetchone()
        assert stored[0] is None
        assert str(stored[1]) == instance["id"]
        snapshot = stored[2]
        assert snapshot["version"] == "1.0.0"
        assert snapshot["instructions"] == "Version one instructions."
        assert snapshot["model_profile"] == "balanced-v1"
        assert snapshot["bindings"]["mikro"]["tenant_integration_id"] == integration["id"]
        assert snapshot["allowed_tools"] == ["mikro.stock.read"]
        internal_name = snapshot["tool_aliases"]["mikro.stock.read"]
        assert internal_name.startswith("connector.")
        connector_tool = snapshot["connector_tools"]["mikro.stock.read"]
        assert connector_tool["binding_key"] == "mikro"
        assert connector_tool["tenant_integration_id"] == integration["id"]
        assert connector_tool["definition_id"] == "mikro"
        assert connector_tool["capability"] == "stock.read"
        assert len(connector_tool["config_fingerprint"]) == 64
        assert connector_tool["credential_reference"] is not None
        assert SECRET not in json.dumps(snapshot)

        governed = connection.execute(
            """SELECT d.server_key,d.remote_name,d.side_effect,d.enabled,p.decision
               FROM tool_definitions d
               LEFT JOIN tool_policies p
                 ON p.workspace_id=d.workspace_id AND p.tool_id=d.id
               WHERE d.workspace_id=%s AND d.name=%s""",
            (space, internal_name),
        ).fetchone()
        assert governed == (
            "connector",
            f"{integration['id']}:stock.read",
            "read",
            True,
            "allow",
        )

        payload = connection.execute(
            "SELECT payload FROM job_outbox WHERE run_id=%s", (run["id"],)
        ).fetchone()[0]
        assert payload["agent_kind"] == "standard"
        assert payload["agent_id"] == instance["id"]

    publish_version(
        client,
        admin,
        "social-media",
        slug,
        "1.1.0",
        required_integrations=["mikro"],
        optional_integrations=["erp"],
        required_tools=[],
        optional_tools=[],
        system_instructions="Version two instructions.",
        model_policy={"primary": "advanced-v2"},
    )
    moved = client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/update",
        json={"version": "1.1.0"},
        headers=owner,
    )
    assert moved.status_code == 200, moved.text

    with psycopg.connect(platform_settings.database_url.get_secret_value()) as connection:
        snapshot = connection.execute(
            "SELECT agent_snapshot FROM agent_runs WHERE id=%s", (run["id"],)
        ).fetchone()[0]
        assert snapshot["version"] == "1.0.0"
        assert snapshot["instructions"] == "Version one instructions."
        assert snapshot["model_profile"] == "balanced-v1"


def test_rebinding_after_queue_fails_closed_before_connector_egress(
    client, admin, keys, platform_settings
):
    owner = headers(keys, "owner-stale-binding")
    space = workspace(client, owner, "Stale binding")
    slug = unique_slug("stale-binding-agent")
    standard_agent(
        client,
        admin,
        slug,
        required_tools=["mikro.stock.read"],
    )
    first = connect_api_key(
        client,
        owner,
        space,
        "mikro",
        "stale-binding-key-0001",
        display_name="Original account",
        account_identifier="original",
    ).json()
    second = connect_api_key(
        client,
        owner,
        space,
        "mikro",
        "stale-binding-key-0002",
        display_name="Replacement account",
        account_identifier="replacement",
    ).json()
    instance = install(
        client,
        owner,
        space,
        slug,
        bindings={"mikro": first["id"]},
    ).json()
    created = client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/runs",
        json={"input": "Read the original account."},
        headers={**owner, "Idempotency-Key": "stale-binding-run-0001"},
    )
    assert created.status_code == 201, created.text

    rebound = client.put(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/bindings/mikro",
        json={"tenant_integration_id": second["id"]},
        headers=owner,
    )
    assert rebound.status_code == 200, rebound.text

    with psycopg.connect(platform_settings.database_url.get_secret_value()) as connection:
        snapshot = connection.execute(
            "SELECT agent_snapshot FROM agent_runs WHERE id=%s", (created.json()["id"],)
        ).fetchone()[0]

    context = SimpleNamespace(
        workspace_id=UUID(space),
        agent_id=UUID(instance["id"]),
        agent_kind="standard",
        agent_snapshot=snapshot,
    )
    adapter = ConnectorMcpAdapter(platform_settings)

    async def exercise():
        await adapter.call_tool_for_context(
            context,
            f"{first['id']}:stock.read",
            {},
            5.0,
        )

    with pytest.raises(McpAdapterError) as raised:
        asyncio.run(exercise())
    assert raised.value.code == "connector_binding_changed"
    assert raised.value.retryable is False


def test_standard_agent_browser_tools_freeze_origin_and_gate_ui_mutation(
    client, admin, keys, platform_settings
):
    browser_settings = platform_settings.model_copy(
        update={
            "browser_runtime_url": "http://browser.internal:8080",
            "browser_runtime_token": SecretStr("x" * 32),
        }
    )
    client.app.state.tenant_agents.settings = browser_settings

    owner = headers(keys, "owner-browser-standard-run")
    space = workspace(client, owner, "Browser standard run")
    slug = unique_slug("browser-standard-agent")
    standard_agent(
        client,
        admin,
        slug,
        required_integrations=[],
        optional_integrations=[],
        required_tools=["browser.page.inspect"],
        optional_tools=["browser.page.action"],
        settings_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"site_url": {"type": "string", "maxLength": 200}},
            "required": ["site_url"],
        },
    )
    installed = install(client, owner, space, slug)
    assert installed.status_code == 201, installed.text
    instance = installed.json()
    configured = client.patch(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}",
        json={"settings": {"site_url": "https://example.com/start"}},
        headers=owner,
    )
    assert configured.status_code == 200, configured.text

    created = client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/runs",
        json={"input": "Inspect the configured site and prepare a bounded change."},
        headers={**owner, "Idempotency-Key": "browser-standard-run-0001"},
    )
    assert created.status_code == 201, created.text

    with psycopg.connect(platform_settings.database_url.get_secret_value()) as connection:
        snapshot = connection.execute(
            "SELECT agent_snapshot FROM agent_runs WHERE id=%s", (created.json()["id"],)
        ).fetchone()[0]
        assert snapshot["browser"] == {"allowed_origin": "https://example.com"}
        assert set(snapshot["allowed_tools"]) == {
            "browser.page.inspect",
            "browser.page.action",
        }
        inspect_name = snapshot["tool_aliases"]["browser.page.inspect"]
        action_name = snapshot["tool_aliases"]["browser.page.action"]
        rows = connection.execute(
            """SELECT d.name,d.remote_name,d.side_effect,p.decision
               FROM tool_definitions d
               JOIN tool_policies p ON p.workspace_id=d.workspace_id AND p.tool_id=d.id
               WHERE d.workspace_id=%s AND d.name=ANY(%s::text[])
               ORDER BY d.remote_name""",
            (space, [inspect_name, action_name]),
        ).fetchall()
        assert rows == [
            (action_name, "page.action", "write", "require_approval"),
            (inspect_name, "page.inspect", "read", "allow"),
        ]


def test_paused_standard_agent_cannot_queue_a_run(client, admin, keys):
    owner = headers(keys, "owner-paused-run")
    space = workspace(client, owner, "Paused run")
    slug = unique_slug("paused-run-agent")
    standard_agent(client, admin, slug)
    instance = install(client, owner, space, slug).json()
    assert instance["status"] == "paused"

    response = client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/runs",
        json={"input": "Try to run before the required account is connected."},
        headers={**owner, "Idempotency-Key": "paused-standard-run-0001"},
    )
    assert response.status_code == 409


def test_installing_with_bindings_activates_immediately(client, admin, keys):
    owner = headers(keys, "owner-install-bound")
    space = workspace(client, owner, "Install bound")
    slug = unique_slug("install-bound-agent")
    standard_agent(client, admin, slug)
    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()

    created = install(client, owner, space, slug, bindings={"mikro": integration["id"]})
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "active"
    assert created.json()["readiness"]["ready"] is True


def test_an_agent_binds_to_an_account_not_to_a_credential(client, admin, keys):
    """Two instances of the same agent serve two customers from one workspace."""
    owner = headers(keys, "owner-multi-account")
    space = workspace(client, owner, "Agency")
    slug = unique_slug("multi-account-agent")
    standard_agent(client, admin, slug)

    customer_a = connect_api_key(
        client,
        owner,
        space,
        "mikro",
        "key-customer-a-0001",
        display_name="Customer A",
        account_identifier="customer-a",
    ).json()
    customer_b = connect_api_key(
        client,
        owner,
        space,
        "mikro",
        "key-customer-b-0002",
        display_name="Customer B",
        account_identifier="customer-b",
    ).json()

    first = install(
        client,
        owner,
        space,
        slug,
        display_name="Customer A social",
        bindings={"mikro": customer_a["id"]},
    ).json()
    second = install(
        client,
        owner,
        space,
        slug,
        display_name="Customer B social",
        bindings={"mikro": customer_b["id"]},
    ).json()

    assert first["bindings"][0]["tenant_integration_id"] == customer_a["id"]
    assert second["bindings"][0]["tenant_integration_id"] == customer_b["id"]
    assert first["bindings"][0]["display_name"] == "Customer A"
    assert second["bindings"][0]["display_name"] == "Customer B"
    # Neither instance carries a secret; it carries a reference to an account.
    assert "key-customer" not in (first | second).__str__()


def test_an_agent_cannot_bind_to_another_tenants_connection(client, admin, keys):
    owner_a, owner_b = headers(keys, "owner-bind-a"), headers(keys, "owner-bind-b")
    space_a, space_b = workspace(client, owner_a, "A"), workspace(client, owner_b, "B")
    slug = unique_slug("cross-tenant-agent")
    standard_agent(client, admin, slug)

    theirs = connect_api_key(client, owner_a, space_a, "mikro", SECRET).json()
    instance = install(client, owner_b, space_b, slug).json()

    stolen = client.put(
        f"/api/v1/workspaces/{space_b}/tenant-agents/{instance['id']}/bindings/mikro",
        json={"tenant_integration_id": theirs["id"]},
        headers=owner_b,
    )
    assert stolen.status_code == 404
    # Installing with the binding supplied up front is refused the same way.
    assert (
        install(
            client, owner_b, space_b, slug, display_name="Second", bindings={"mikro": theirs["id"]}
        ).status_code
        == 404
    )

    current = client.get(
        f"/api/v1/workspaces/{space_b}/tenant-agents/{instance['id']}", headers=owner_b
    ).json()
    assert current["bindings"] == []
    assert current["status"] == "paused"


def test_a_binding_must_match_the_integration_it_stands_for(client, admin, keys):
    owner = headers(keys, "owner-binding-shape")
    space = workspace(client, owner, "Binding shape")
    slug = unique_slug("binding-shape-agent")
    standard_agent(client, admin, slug)
    erp = client.post(
        f"/api/v1/workspaces/{space}/integrations",
        json={
            "integration_definition_id": "erp",
            "display_name": "ERP",
            "account_identifier": "erp-1",
            "credentials": {"api_key": SECRET, "base_url": "https://erp.example.com"},
        },
        headers=owner,
    ).json()
    instance = install(client, owner, space, slug).json()

    wrong_service = client.put(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/bindings/mikro",
        json={"tenant_integration_id": erp["id"]},
        headers=owner,
    )
    assert wrong_service.status_code == 422

    undeclared = client.put(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/bindings/instagram",
        json={"tenant_integration_id": erp["id"]},
        headers=owner,
    )
    assert undeclared.status_code == 422


def test_a_disabled_connection_pauses_the_agents_that_depend_on_it(client, admin, keys):
    owner = headers(keys, "owner-disable")
    space = workspace(client, owner, "Disable")
    slug = unique_slug("disable-agent")
    standard_agent(client, admin, slug)
    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()
    instance = install(client, owner, space, slug, bindings={"mikro": integration["id"]}).json()
    assert instance["status"] == "active"

    disabled = client.patch(
        f"/api/v1/workspaces/{space}/integrations/{integration['id']}",
        json={"enabled": False},
        headers=owner,
    )
    assert disabled.status_code == 200
    after = client.get(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}", headers=owner
    ).json()
    assert after["readiness"]["ready"] is False
    # An agent may not be activated against a connection the tenant switched off.
    assert (
        client.patch(
            f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}",
            json={"status": "active"},
            headers=owner,
        ).status_code
        == 409
    )
    # A connection an agent still uses cannot be removed out from under it.
    assert (
        client.delete(
            f"/api/v1/workspaces/{space}/integrations/{integration['id']}", headers=owner
        ).status_code
        == 409
    )


def test_a_pause_a_person_asked_for_is_not_undone_by_the_platform(client, admin, keys):
    """Readiness resumes an agent the platform paused, never one someone paused."""
    owner = headers(keys, "owner-explicit-pause")
    space = workspace(client, owner, "Explicit pause")
    slug = unique_slug("pause-agent")
    standard_agent(client, admin, slug)
    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()
    instance = install(client, owner, space, slug, bindings={"mikro": integration["id"]}).json()
    assert instance["status"] == "active"

    paused = client.patch(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}",
        json={"status": "paused"},
        headers=owner,
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"

    # Rebinding the same connection keeps the agent paused: the decision stands.
    rebound = client.put(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/bindings/mikro",
        json={"tenant_integration_id": integration["id"]},
        headers=owner,
    )
    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["status"] == "paused"
    assert rebound.json()["readiness"]["ready"] is True

    resumed = client.patch(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}",
        json={"status": "active"},
        headers=owner,
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "active"


def test_a_removed_agent_frees_its_name_for_a_fresh_install(client, admin, keys):
    owner = headers(keys, "owner-reinstall")
    space = workspace(client, owner, "Reinstall")
    slug = unique_slug("reinstall-agent")
    standard_agent(client, admin, slug)
    first = install(client, owner, space, slug, display_name="Customer A social").json()
    assert (
        client.delete(
            f"/api/v1/workspaces/{space}/tenant-agents/{first['id']}", headers=owner
        ).status_code
        == 204
    )

    again = install(client, owner, space, slug, display_name="Customer A social")
    assert again.status_code == 201, again.text
    assert again.json()["id"] != first["id"]
    # The removed instance keeps its own record of what ran when.
    history = client.get(
        f"/api/v1/workspaces/{space}/tenant-agents/{first['id']}/history", headers=owner
    ).json()["items"]
    assert [item["action"] for item in history] == ["installed"]


def test_each_tenant_moves_between_versions_on_its_own(client, admin, keys):
    """The central requirement: one release does not move every customer at once."""
    slug = unique_slug("pinning-agent")
    standard_agent(client, admin, slug, "1.3.0")
    publish_version(
        client,
        admin,
        "social-media",
        slug,
        "1.4.0",
        required_integrations=["mikro"],
        required_tools=[],
        optional_tools=[],
        optional_integrations=["erp"],
    )

    tenants = {}
    for name in ("a", "b", "c"):
        owner = headers(keys, f"owner-pin-{name}")
        space = workspace(client, owner, f"Pin {name}")
        instance = install(client, owner, space, slug).json()
        tenants[name] = (owner, space, instance["id"])
        assert instance["version"] == "1.4.0"

    publish_version(
        client,
        admin,
        "social-media",
        slug,
        "1.5.0",
        required_integrations=["mikro"],
        required_tools=[],
        optional_tools=[],
        optional_integrations=["erp"],
        changelog="Publishing retry fixed.",
    )

    def version_of(name):
        owner, space, agent_id = tenants[name]
        return client.get(
            f"/api/v1/workspaces/{space}/tenant-agents/{agent_id}", headers=owner
        ).json()

    # A new release changes nothing until each tenant chooses to take it.
    for name in tenants:
        current = version_of(name)
        assert current["version"] == "1.4.0"
        assert current["available_version"] == "1.5.0"

    owner_a, space_a, agent_a = tenants["a"]
    moved = client.post(
        f"/api/v1/workspaces/{space_a}/tenant-agents/{agent_a}/update", json={}, headers=owner_a
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["version"] == "1.5.0"

    owner_b, space_b, agent_b = tenants["b"]
    pinned_back = client.post(
        f"/api/v1/workspaces/{space_b}/tenant-agents/{agent_b}/update",
        json={"version": "1.3.0"},
        headers=owner_b,
    )
    assert pinned_back.status_code == 200, pinned_back.text

    assert version_of("a")["version"] == "1.5.0"
    assert version_of("b")["version"] == "1.3.0"
    assert version_of("c")["version"] == "1.4.0"


def test_automatic_update_mode_moves_only_opted_in_instances(client, admin, keys):
    owner = headers(keys, "owner-auto-update")
    space = workspace(client, owner, "Automatic updates")
    slug = unique_slug("automatic-update-agent")
    standard_agent(client, admin, slug, "1.0.0")

    automatic = install(
        client, owner, space, slug, display_name="Automatic", update_mode="automatic"
    ).json()
    manual = install(client, owner, space, slug, display_name="Manual", update_mode="manual").json()

    publish_version(
        client,
        admin,
        "social-media",
        slug,
        "1.1.0",
        required_integrations=["mikro"],
        optional_integrations=["erp"],
        required_tools=[],
        optional_tools=[],
    )

    updated, cursor = asyncio.run(client.app.state.tenant_agents.apply_automatic_updates(limit=50))
    assert updated == 1
    assert cursor is None

    automatic_now = client.get(
        f"/api/v1/workspaces/{space}/tenant-agents/{automatic['id']}", headers=owner
    ).json()
    manual_now = client.get(
        f"/api/v1/workspaces/{space}/tenant-agents/{manual['id']}", headers=owner
    ).json()
    assert automatic_now["version"] == "1.1.0"
    assert manual_now["version"] == "1.0.0"

    history = client.get(
        f"/api/v1/workspaces/{space}/tenant-agents/{automatic['id']}/history", headers=owner
    ).json()["items"]
    assert [item["action"] for item in history] == ["installed", "updated"]
    assert history[-1]["actor_subject"] == "agent-update-scheduler"

    # Re-running the scheduler is idempotent when the offered version is already pinned.
    second, _ = asyncio.run(client.app.state.tenant_agents.apply_automatic_updates(limit=50))
    assert second == 0


def test_rollback_restores_the_previous_version_and_keeps_every_setting(client, admin, keys):
    owner = headers(keys, "owner-agent-rollback")
    space = workspace(client, owner, "Rollback")
    slug = unique_slug("rollback-agent")
    standard_agent(client, admin, slug, "1.0.0")
    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()
    instance = install(client, owner, space, slug, bindings={"mikro": integration["id"]}).json()

    configured = client.patch(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}",
        json={
            "display_name": "Arma social",
            "instructions_override": "Always write in Turkish.",
            "settings": {"brand_voice": "warm", "primary_language": "tr"},
        },
        headers=owner,
    )
    assert configured.status_code == 200, configured.text

    publish_version(
        client,
        admin,
        "social-media",
        slug,
        "2.0.0",
        required_integrations=["mikro"],
        optional_integrations=["erp"],
        required_tools=[],
        optional_tools=[],
    )
    updated = client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/update",
        json={"version": "2.0.0"},
        headers=owner,
    ).json()
    assert updated["version"] == "2.0.0"
    # A version move never touches what the tenant configured.
    assert updated["display_name"] == "Arma social"
    assert updated["instructions_override"] == "Always write in Turkish."
    assert updated["settings"] == {"brand_voice": "warm", "primary_language": "tr"}
    assert updated["bindings"][0]["tenant_integration_id"] == integration["id"]

    restored = client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/rollback", headers=owner
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["version"] == "1.0.0"
    assert restored.json()["display_name"] == "Arma social"
    assert restored.json()["instructions_override"] == "Always write in Turkish."
    assert restored.json()["settings"] == {"brand_voice": "warm", "primary_language": "tr"}
    assert restored.json()["bindings"][0]["tenant_integration_id"] == integration["id"]
    assert restored.json()["status"] == "active"

    history = client.get(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/history", headers=owner
    ).json()["items"]
    assert [item["action"] for item in history] == ["installed", "updated", "rolled_back"]
    assert history[-1]["from_version"] == "2.0.0"
    assert history[-1]["to_version"] == "1.0.0"


def test_a_fork_stops_following_the_catalog(client, admin, keys):
    owner = headers(keys, "owner-fork")
    space = workspace(client, owner, "Fork")
    slug = unique_slug("fork-agent")
    standard_agent(client, admin, slug, "1.0.0", system_instructions="Original behaviour.")
    instance = install(client, owner, space, slug).json()

    forked = client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/fork",
        json={"name": "Arma Social Media Agent"},
        headers=owner,
    )
    assert forked.status_code == 201, forked.text
    custom = forked.json()
    assert custom["name"] == "Arma Social Media Agent"
    assert custom["instructions"] == "Original behaviour."
    assert custom["origin_version"] == "1.0.0"

    # The catalog moves on; the fork does not.
    publish_version(
        client,
        admin,
        "social-media",
        slug,
        "2.0.0",
        required_integrations=["mikro"],
        optional_integrations=["erp"],
        required_tools=[],
        optional_tools=[],
        system_instructions="Completely different behaviour.",
    )
    client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/update",
        json={"version": "2.0.0"},
        headers=owner,
    )
    unchanged = client.get(
        f"/api/v1/workspaces/{space}/agents/{custom['id']}", headers=owner
    ).json()
    assert unchanged["instructions"] == "Original behaviour."

    # A fork lives in the workspace's own agent list, visible only there.
    listing = client.get(f"/api/v1/workspaces/{space}/agents?limit=100", headers=owner).json()
    assert custom["id"] in {item["id"] for item in listing["items"]}


def test_an_agent_requiring_a_newer_runtime_is_not_installed(
    client, admin, keys, platform_settings
):
    owner = headers(keys, "owner-runtime")
    space = workspace(client, owner, "Runtime")
    slug = unique_slug("future-agent")
    standard_agent(client, admin, slug, "1.0.0", min_runtime_version="99.0.0")

    refused = install(client, owner, space, slug)
    assert refused.status_code == 409
    assert platform_settings.agent_runtime_version == "1.0.0"


def test_a_normal_user_may_use_agents_but_not_install_or_configure_them(client, admin, keys):
    owner, member = headers(keys, "owner-agent-roles"), headers(keys, "member-agent-roles")
    space = workspace(client, owner, "Agent roles")
    client.put(
        f"/api/v1/workspaces/{space}/members",
        json={"subject": "member-agent-roles", "role": "member"},
        headers=owner,
    )
    slug = unique_slug("roles-agent")
    standard_agent(client, admin, slug)
    instance = install(client, owner, space, slug).json()

    # Reading the catalog and the installed agents is part of using them.
    assert catalog_entry(client, member, space, slug) is not None
    assert (
        client.get(
            f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}", headers=member
        ).status_code
        == 200
    )
    assert install(client, member, space, slug, display_name="Mine").status_code == 403
    assert (
        client.patch(
            f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}",
            json={"display_name": "Renamed"},
            headers=member,
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/fork",
            json={"name": "Mine"},
            headers=member,
        ).status_code
        == 403
    )


def test_agent_lifecycle_changes_are_audited(client, admin, keys, platform_settings):
    owner = headers(keys, "owner-agent-audit", request_id="agent-audit")
    space = workspace(client, owner, "Agent audit")
    slug = unique_slug("audit-agent")
    standard_agent(client, admin, slug, "1.0.0")
    integration = connect_api_key(client, owner, space, "mikro", SECRET).json()
    instance = install(client, owner, space, slug, bindings={"mikro": integration["id"]}).json()
    assert instance["version"] == "1.0.0"
    # Published after the install, so taking it is a decision this tenant records.
    publish_version(
        client,
        admin,
        "social-media",
        slug,
        "1.1.0",
        required_integrations=["mikro"],
        optional_integrations=["erp"],
        required_tools=[],
        optional_tools=[],
    )
    moved = client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/update",
        json={"version": "1.1.0"},
        headers=owner,
    )
    assert moved.status_code == 200, moved.text
    restored = client.post(
        f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}/rollback", headers=owner
    )
    assert restored.status_code == 200, restored.text
    client.delete(f"/api/v1/workspaces/{space}/tenant-agents/{instance['id']}", headers=owner)

    with psycopg.connect(platform_settings.database_url.get_secret_value()) as connection:
        actions = {
            row[0]
            for row in connection.execute(
                "SELECT action FROM security_events WHERE workspace_id=%s", (space,)
            ).fetchall()
        }
        assert {
            "integration.added",
            "agent.installed",
            "agent.binding_changed",
            "agent.updated",
            "agent.rolled_back",
            "agent.disabled",
        } <= actions
        # Version history is append-only, so what ran when cannot be rewritten.
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("DELETE FROM tenant_agent_version_events")
