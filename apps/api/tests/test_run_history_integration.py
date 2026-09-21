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
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]


def test_run_history_is_scoped_filtered_and_paginated(keys, auth_settings):
    migrate(auth_settings)
    prefix = str(uuid4())
    owner, member, outsider = [prefix + suffix for suffix in ("owner", "member", "outsider")]

    def headers(subject, idempotency_key=None):
        result = {
            "Authorization": "Bearer " + token(keys, subject),
            "x-request-id": "run-history-integration",
        }
        if idempotency_key:
            result["Idempotency-Key"] = idempotency_key
        return result

    with TestClient(create_app(settings=auth_settings)) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"name": "History workspace"}, headers=headers(owner)
        ).json()["id"]
        base = "/api/v1/workspaces/" + workspace_id
        assert (
            client.put(
                base + "/members",
                json={"subject": member, "role": "member"},
                headers=headers(owner),
            ).status_code
            == 200
        )

        agents = {}
        for name in ("Research agent", "Support agent"):
            created = client.post(
                base + "/agents",
                json={"name": name, "instructions": "Answer with citations."},
                headers=headers(owner),
            )
            assert created.status_code == 201, created.text
            agents[name] = created.json()["id"]

        runs = []
        for index in range(3):
            created = client.post(
                base + "/runs",
                json={"agent_id": agents["Research agent"], "input": f"Task {index}"},
                headers=headers(owner, f"history-owner-{index:04d}"),
            )
            assert created.status_code == 201, created.text
            runs.append(created.json()["id"])
        member_run = client.post(
            base + "/runs",
            json={"agent_id": agents["Support agent"], "input": "Member task"},
            headers=headers(member, "history-member-0001"),
        )
        assert member_run.status_code == 201, member_run.text
        member_run_id = member_run.json()["id"]

        history = client.get(base + "/runs", headers=headers(owner))
        assert history.status_code == 200, history.text
        body = history.json()
        assert [item["id"] for item in body["items"]] == [member_run_id, *reversed(runs)]
        assert body["next_cursor"] is None
        newest = body["items"][0]
        # History carries workspace metadata only; the prompt stays behind the result route.
        assert newest["agent_name"] == "Support agent"
        assert newest["status"] == "queued"
        assert newest["requested_by_me"] is False
        assert "input_text" not in newest and "requested_by_subject" not in newest

        member_view = client.get(base + "/runs", headers=headers(member)).json()
        assert [item["requested_by_me"] for item in member_view["items"]] == [
            True,
            False,
            False,
            False,
        ]
        mine = client.get(base + "/runs?requested_by_me=true", headers=headers(member)).json()
        assert [item["id"] for item in mine["items"]] == [member_run_id]

        by_agent = client.get(
            base + "/runs?agent_id=" + agents["Research agent"], headers=headers(owner)
        ).json()
        assert [item["id"] for item in by_agent["items"]] == list(reversed(runs))
        assert client.get(base + "/runs?status=succeeded", headers=headers(owner)).json() == {
            "items": [],
            "next_cursor": None,
        }
        assert (
            client.get(base + "/runs?status=not-a-status", headers=headers(owner)).status_code
            == 422
        )
        assert client.get(base + "/runs?limit=0", headers=headers(owner)).status_code == 422

        first_page = client.get(base + "/runs?limit=2", headers=headers(owner)).json()
        assert [item["id"] for item in first_page["items"]] == [member_run_id, runs[2]]
        assert first_page["next_cursor"] == runs[2]
        second_page = client.get(
            base + "/runs?limit=2&cursor=" + first_page["next_cursor"], headers=headers(owner)
        ).json()
        assert [item["id"] for item in second_page["items"]] == [runs[1], runs[0]]
        assert second_page["next_cursor"] is None
        assert (
            client.get(base + "/runs?cursor=" + str(uuid4()), headers=headers(owner)).status_code
            == 404
        )

        # Tenant isolation: a non-member sees the workspace as absent, not empty.
        assert client.get(base + "/runs", headers=headers(outsider)).status_code == 404
        other_workspace = client.post(
            "/api/v1/workspaces", json={"name": "Other workspace"}, headers=headers(outsider)
        ).json()["id"]
        other_history = client.get(
            f"/api/v1/workspaces/{other_workspace}/runs", headers=headers(outsider)
        ).json()
        assert other_history["items"] == []
        assert (
            client.get(
                f"/api/v1/workspaces/{other_workspace}/runs?cursor=" + runs[0],
                headers=headers(outsider),
            ).status_code
            == 404
        )

    # A terminal run keeps its history row with the recorded failure code.
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        connection.execute(
            "UPDATE agent_runs SET status='running',updated_at=now() WHERE id=%s", (runs[0],)
        )
        connection.execute(
            """UPDATE agent_runs
               SET status='failed',failure_code='model_error',finished_at=now(),updated_at=now()
               WHERE id=%s""",
            (runs[0],),
        )
        connection.commit()

    with TestClient(create_app(settings=auth_settings)) as client:
        failed = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs?status=failed", headers=headers(owner)
        ).json()
        assert [item["id"] for item in failed["items"]] == [runs[0]]
        assert failed["items"][0]["failure_code"] == "model_error"
        assert failed["items"][0]["finished_at"] is not None
