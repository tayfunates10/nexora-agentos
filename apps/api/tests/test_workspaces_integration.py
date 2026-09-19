import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_auth import token

from nexora_api.main import create_app
from nexora_api.migrate import migrate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL"),
]


def test_workspace_authorization_and_audit(keys, auth_settings):
    migrate(auth_settings)
    migrate(auth_settings)  # Repeated migration is safe and checksum-verified.
    prefix = str(uuid4())
    owner, member, admin, outsider = [
        prefix + name for name in ["owner", "member", "admin", "other"]
    ]

    def headers(subject):
        return {
            "Authorization": "Bearer " + token(keys, subject, role="owner"),
            "x-request-id": "integration-rbac",
        }

    with TestClient(create_app(settings=auth_settings)) as client:
        created = client.post(
            "/api/v1/workspaces", json={"name": "Workspace A"}, headers=headers(owner)
        )
        assert created.status_code == 201, created.text
        workspace_id = created.json()["id"]
        base = "/api/v1/workspaces/" + workspace_id
        assert client.get(base, headers=headers(outsider)).status_code == 404
        assert (
            client.patch(base, json={"name": "stolen"}, headers=headers(outsider)).status_code
            == 404
        )
        assert client.get("/api/v1/workspaces", headers=headers(outsider)).json()["items"] == []
        for subject, role in [(member, "member"), (admin, "admin")]:
            result = client.put(
                base + "/members", json={"subject": subject, "role": role}, headers=headers(owner)
            )
            assert result.status_code == 200, result.text
        assert client.get(base, headers=headers(member)).status_code == 200
        assert (
            client.patch(base, json={"name": "denied"}, headers=headers(member)).status_code == 403
        )
        assert (
            client.patch(base, json={"name": "renamed"}, headers=headers(admin)).status_code == 200
        )
        assert (
            client.put(
                base + "/members", json={"subject": member, "role": "admin"}, headers=headers(admin)
            ).status_code
            == 403
        )
        assert (
            client.put(
                base + "/members", json={"subject": owner, "role": "member"}, headers=headers(owner)
            ).status_code
            == 403
        )
        assert (
            client.put(
                base + "/members", json={"subject": member, "role": "owner"}, headers=headers(owner)
            ).status_code
            == 403
        )
        # Demotion is effective on the very next request despite the token claiming owner.
        assert (
            client.put(
                base + "/members", json={"subject": admin, "role": "member"}, headers=headers(owner)
            ).status_code
            == 200
        )
        assert (
            client.patch(base, json={"name": "denied"}, headers=headers(admin)).status_code == 403
        )
        assert (
            client.post(
                "/api/v1/workspaces", json={"name": "  "}, headers=headers(owner)
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/workspaces", json={"name": "X", "role": "owner"}, headers=headers(owner)
            ).status_code
            == 422
        )
        assert client.get("/api/v1/workspaces?limit=101", headers=headers(owner)).status_code == 422
        assert (
            client.post(
                "/api/v1/workspaces", json={"name": "Workspace B"}, headers=headers(owner)
            ).status_code
            == 201
        )
        page1 = client.get("/api/v1/workspaces?limit=1", headers=headers(owner)).json()
        page2 = client.get(
            "/api/v1/workspaces?limit=1&cursor=" + page1["next_cursor"], headers=headers(owner)
        ).json()
        assert len(page1["items"]) == len(page2["items"]) == 1
        assert page1["items"][0]["id"] != page2["items"][0]["id"]
        assert page2["next_cursor"] is None

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        events = connection.execute(
            "SELECT action,request_id FROM security_events WHERE workspace_id=%s", (workspace_id,)
        ).fetchall()
        assert len(events) == 5  # create, two grants, rename, demotion; denied writes never commit
        assert all(row[1] == "integration-rbac" for row in events)
        # Same subject under a different issuer does not share memberships.
        import asyncio

        from nexora_api.auth import Principal
        from nexora_api.workspace_repository import WorkspaceRepository

        rows = asyncio.run(
            WorkspaceRepository(auth_settings).list(
                Principal("https://other.test", owner), 10, None
            )
        )
        assert rows == []
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute("DELETE FROM security_events WHERE workspace_id=%s", (workspace_id,))


def test_audit_failure_rolls_back_workspace(keys, auth_settings, monkeypatch):
    from nexora_api.workspace_repository import WorkspaceRepository

    migrate(auth_settings)
    subject = str(uuid4())

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(WorkspaceRepository, "audit", fail_audit)
    with TestClient(create_app(settings=auth_settings)) as client:
        response = client.post(
            "/api/v1/workspaces",
            json={"name": "Must roll back"},
            headers={"Authorization": "Bearer " + token(keys, subject)},
        )
        assert response.status_code == 500
        rows = client.get(
            "/api/v1/workspaces", headers={"Authorization": "Bearer " + token(keys, subject)}
        )
        assert rows.json()["items"] == []
