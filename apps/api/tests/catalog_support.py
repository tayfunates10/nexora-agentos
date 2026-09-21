"""Helpers shared by the catalog, vault and tenant agent integration tests."""

import json
from pathlib import Path
from uuid import uuid4

from test_auth import token

REPOSITORY = Path(__file__).resolve().parents[3]


def unique_slug(base: str) -> str:
    """A catalog slug of this run only, so repeated runs never collide on a published name."""
    return f"{base}-{uuid4().hex[:10]}"


def headers(keys, subject, request_id="integration-catalog"):
    return {
        "Authorization": "Bearer " + token(keys, subject),
        "x-request-id": request_id,
    }


def connector_package(connector_id: str) -> dict:
    return json.loads((REPOSITORY / "connectors" / connector_id / "connector.json").read_text())


def agent_package(slug: str) -> dict:
    return json.loads((REPOSITORY / "agents" / slug / "manifest.json").read_text())


def publish_connector(client, admin, connector_id: str, **overrides) -> dict:
    """Publish a shipped connector definition through the platform registry API."""
    document = {**connector_package(connector_id), **overrides}
    response = client.put(
        f"/api/v1/platform/integrations/{document['id']}", json=document, headers=admin
    )
    assert response.status_code == 200, response.text
    return response.json()


def manifest_for(package: str, slug: str, **overrides) -> dict:
    """A shipped manifest re-slugged, so each test publishes into its own catalog entry."""
    return {
        **agent_package(package),
        "slug": slug,
        "id": f"nexora.{slug}",
        **overrides,
    }


def create_catalog_agent(client, admin, package: str, slug: str | None = None, **overrides) -> dict:
    shipped = agent_package(package)
    body = {
        "slug": slug or shipped["slug"],
        "name": shipped["name"],
        "description": shipped["description"],
        "category": shipped["category"],
        "icon": shipped["icon"],
        "status": "stable",
        "visibility": "public",
        **overrides,
    }
    response = client.post("/api/v1/platform/agents", json=body, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()


def publish_version(
    client, admin, package: str, slug: str, version: str | None = None, **overrides
) -> dict:
    """Publish one immutable version of a standard agent from its shipped manifest."""
    manifest = manifest_for(package, slug, **overrides)
    if version:
        manifest["version"] = version
    response = client.post(
        f"/api/v1/platform/agents/{slug}/versions", json={"manifest": manifest}, headers=admin
    )
    assert response.status_code == 201, response.text
    return response.json()


def catalog_entry(client, caller, workspace_id: str, slug: str) -> dict | None:
    """What this workspace is currently offered for one standard agent."""
    listing = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent-catalog?limit=100", headers=caller
    )
    assert listing.status_code == 200, listing.text
    return next((item for item in listing.json()["items"] if item["slug"] == slug), None)


def workspace(client, owner_headers, name="Integration workspace") -> str:
    created = client.post("/api/v1/workspaces", json={"name": name}, headers=owner_headers)
    assert created.status_code == 201, created.text
    return created.json()["id"]


def connect_api_key(client, caller, workspace_id, definition_id, secret, **overrides) -> dict:
    """Connect a key-based integration the way the console does."""
    body = {
        "integration_definition_id": definition_id,
        "display_name": overrides.pop("display_name", "Primary account"),
        "account_identifier": overrides.pop("account_identifier", "acct-1"),
        "credentials": {"api_key": secret, "base_url": "https://erp.example.com"},
        **overrides,
    }
    return client.post(f"/api/v1/workspaces/{workspace_id}/integrations", json=body, headers=caller)
