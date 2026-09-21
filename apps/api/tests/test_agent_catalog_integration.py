import os

import pytest
from catalog_support import (
    catalog_entry,
    create_catalog_agent,
    headers,
    manifest_for,
    publish_version,
    unique_slug,
    workspace,
)
from conftest import PLATFORM_ADMIN
from fastapi.testclient import TestClient

from nexora_api.main import create_app
from nexora_api.migrate import migrate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL"),
]


@pytest.fixture
def client(platform_settings):
    migrate(platform_settings)
    with TestClient(create_app(settings=platform_settings)) as running:
        yield running


def test_agent_studio_is_invisible_to_everyone_but_the_platform_operator(client, keys):
    """A tenant owner is not a platform administrator, and is not told the surface exists."""
    tenant = headers(keys, "tenant-owner-studio")
    for method, path in [
        ("get", "/api/v1/platform/agents"),
        ("get", "/api/v1/platform/agents/social-media"),
        ("post", "/api/v1/platform/agents"),
    ]:
        response = getattr(client, method)(
            path, **({"json": {}} if method == "post" else {}), headers=tenant
        )
        assert response.status_code == 404, f"{path}: {response.status_code}"

    assert client.get("/api/v1/platform/agents").status_code == 401


def test_a_published_version_is_immutable_and_only_moves_forward(client, keys):
    admin = headers(keys, PLATFORM_ADMIN)
    slug = unique_slug("social-media")
    create_catalog_agent(client, admin, "social-media", slug)
    publish_version(client, admin, "social-media", slug, "1.4.2")

    # The same number can never be published twice: a tenant pinned to 1.4.2 must always
    # get the 1.4.2 that was reviewed.
    repeat = client.post(
        f"/api/v1/platform/agents/{slug}/versions",
        json={"manifest": manifest_for("social-media", slug, version="1.4.2")},
        headers=admin,
    )
    assert repeat.status_code == 409

    backwards = client.post(
        f"/api/v1/platform/agents/{slug}/versions",
        json={"manifest": manifest_for("social-media", slug, version="1.4.1")},
        headers=admin,
    )
    assert backwards.status_code == 409

    publish_version(client, admin, "social-media", slug, "1.9.0")
    publish_version(client, admin, "social-media", slug, "1.10.0")
    listed = client.get(f"/api/v1/platform/agents/{slug}/versions", headers=admin).json()
    # Newest first, ordered numerically rather than as text.
    assert [item["version"] for item in listed["items"]] == ["1.10.0", "1.9.0", "1.4.2"]


def test_a_manifest_must_match_the_catalog_entry_it_is_published_to(client, keys):
    admin = headers(keys, PLATFORM_ADMIN)
    slug = unique_slug("seo")
    create_catalog_agent(client, admin, "seo", slug)
    mismatched = client.post(
        f"/api/v1/platform/agents/{slug}/versions",
        json={"manifest": manifest_for("social-media", "social-media-elsewhere")},
        headers=admin,
    )
    assert mismatched.status_code == 422


def test_each_agent_versions_on_its_own_clock(client, keys):
    """Two standard agents move independently; neither release touches the other."""
    admin = headers(keys, PLATFORM_ADMIN)
    social, seo = unique_slug("social-media"), unique_slug("seo")
    create_catalog_agent(client, admin, "social-media", social)
    create_catalog_agent(client, admin, "seo", seo)
    publish_version(client, admin, "social-media", social, "1.8.2")
    publish_version(client, admin, "seo", seo, "2.4.1")
    publish_version(client, admin, "social-media", social, "1.8.3")

    listing = client.get("/api/v1/platform/agents?limit=100", headers=admin).json()["items"]
    versions = {item["slug"]: item["latest_version"] for item in listing}
    assert versions[social] == "1.8.3"
    assert versions[seo] == "2.4.1"


def test_a_restricted_agent_reaches_only_the_workspaces_it_was_granted_to(client, keys):
    admin = headers(keys, PLATFORM_ADMIN)
    owner_a, owner_b = headers(keys, "owner-entitle-a"), headers(keys, "owner-entitle-b")
    space_a, space_b = workspace(client, owner_a, "A"), workspace(client, owner_b, "B")
    slug = unique_slug("operations")
    create_catalog_agent(client, admin, "operations", slug, visibility="restricted")
    publish_version(client, admin, "operations", slug, "1.0.0", status="stable", channel="stable")

    def visible(caller, space):
        return catalog_entry(client, caller, space, slug) is not None

    assert not visible(owner_a, space_a)
    granted = client.put(
        f"/api/v1/platform/agents/{slug}/entitlements",
        json={"workspace_id": space_a},
        headers=admin,
    )
    assert granted.status_code == 201, granted.text
    assert visible(owner_a, space_a)
    # The grant is per workspace and reaches no one else.
    assert not visible(owner_b, space_b)

    revoked = client.delete(f"/api/v1/platform/agents/{slug}/entitlements/{space_a}", headers=admin)
    assert revoked.status_code == 204
    assert not visible(owner_a, space_a)


def test_a_staged_rollout_reaches_part_of_the_estate_then_all_of_it(client, keys):
    admin = headers(keys, PLATFORM_ADMIN)
    slug = unique_slug("reporting")
    create_catalog_agent(client, admin, "reporting", slug)
    publish_version(client, admin, "reporting", slug, "1.0.0")
    publish_version(client, admin, "reporting", slug, "1.1.0")

    spaces = []
    for index in range(40):
        owner = headers(keys, f"owner-rollout-{index}")
        spaces.append((owner, workspace(client, owner, f"Rollout {index}")))

    def offered():
        return [
            catalog_entry(client, owner, space, slug)["available_version"]
            for owner, space in spaces
        ]

    # With no rollout the channel simply serves its newest released version.
    assert set(offered()) == {"1.1.0"}

    publish_version(client, admin, "reporting", slug, "1.2.0")
    staged = client.post(
        f"/api/v1/platform/agents/{slug}/rollouts",
        json={"version": "1.2.0", "channel": "stable", "percentage": 25},
        headers=admin,
    )
    assert staged.status_code == 201, staged.text
    partial = offered()
    # Some workspaces are inside the rollout; the rest keep the version they had.
    assert set(partial) == {"1.1.0", "1.2.0"}, partial
    assert 0 < partial.count("1.2.0") < len(spaces)

    widened = client.patch(
        f"/api/v1/platform/agents/{slug}/rollouts/{staged.json()['id']}",
        json={"percentage": 100},
        headers=admin,
    )
    assert widened.status_code == 200, widened.text
    assert set(offered()) == {"1.2.0"}


def test_rolling_back_a_rollout_restores_the_version_it_replaced(client, keys):
    admin = headers(keys, PLATFORM_ADMIN)
    owner = headers(keys, "owner-rollback-platform")
    space = workspace(client, owner, "Rollback")
    slug = unique_slug("documents")
    create_catalog_agent(client, admin, "documents", slug)
    for version in ("1.0.0", "1.1.0"):
        publish_version(client, admin, "documents", slug, version)

    def offered():
        return catalog_entry(client, owner, space, slug)["available_version"]

    first = client.post(
        f"/api/v1/platform/agents/{slug}/rollouts",
        json={"version": "1.0.0", "channel": "stable", "percentage": 100},
        headers=admin,
    ).json()
    assert offered() == "1.0.0"

    second = client.post(
        f"/api/v1/platform/agents/{slug}/rollouts",
        json={"version": "1.1.0", "channel": "stable", "percentage": 100},
        headers=admin,
    ).json()
    assert offered() == "1.1.0"

    undone = client.post(
        f"/api/v1/platform/agents/{slug}/rollouts/{second['id']}/rollback", headers=admin
    )
    assert undone.status_code == 200, undone.text
    assert undone.json()["version"] == "1.0.0"
    assert offered() == "1.0.0"

    history = client.get(
        f"/api/v1/platform/agents/{slug}/rollouts?limit=100", headers=admin
    ).json()["items"]
    states = {item["id"]: item["state"] for item in history}
    # The failed release is retired, not erased, and the restored one is active.
    assert states[second["id"]] == "rolled_back"
    assert states[first["id"]] in ("completed", "active")
    assert undone.json()["state"] == "active"


def test_a_canary_channel_sees_a_release_a_stable_channel_does_not(client, keys):
    admin = headers(keys, PLATFORM_ADMIN)
    slug = unique_slug("sales-crm")
    create_catalog_agent(client, admin, "sales-crm", slug)
    publish_version(client, admin, "sales-crm", slug, "1.0.0", channel="stable", status="stable")
    publish_version(client, admin, "sales-crm", slug, "2.0.0", channel="canary", status="beta")

    versions = {}
    for channel in ("stable", "beta", "canary"):
        owner = headers(keys, f"owner-channel-{channel}")
        space = workspace(client, owner, f"Channel {channel}")
        policy = client.put(
            f"/api/v1/workspaces/{space}/agent-update-policy",
            json={"channel": channel, "mode": "manual"},
            headers=owner,
        )
        assert policy.status_code == 200, policy.text
        versions[channel] = catalog_entry(client, owner, space, slug)["available_version"]

    assert versions == {"stable": "1.0.0", "beta": "1.0.0", "canary": "2.0.0"}


def test_platform_actions_are_written_to_an_append_only_journal(client, keys, platform_settings):
    import psycopg

    admin = headers(keys, PLATFORM_ADMIN, request_id="audit-journal")
    slug = unique_slug("customer-support")
    create_catalog_agent(client, admin, "customer-support", slug)
    publish_version(client, admin, "customer-support", slug, "1.0.0")

    with psycopg.connect(platform_settings.database_url.get_secret_value()) as connection:
        rows = connection.execute(
            "SELECT action,target,actor_subject FROM platform_events WHERE request_id=%s",
            ("audit-journal",),
        ).fetchall()
        actions = {row[0] for row in rows}
        assert {"catalog.agent.created", "catalog.version.published"} <= actions
        assert all(row[2] == PLATFORM_ADMIN for row in rows)
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("DELETE FROM platform_events")
