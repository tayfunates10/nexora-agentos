import asyncio
import json
import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from test_auth import token

from nexora_api.main import create_app
from nexora_api.migrate import migrate
from nexora_api.outbox import QUEUE_STREAM, OutboxPublisher

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]


def test_agent_definitions_runs_idempotency_and_outbox(keys, auth_settings):
    migrate(auth_settings)
    prefix = str(uuid4())
    owner, admin, member, outsider = [
        prefix + suffix for suffix in ("owner", "admin", "member", "outsider")
    ]

    def headers(subject, idempotency_key=None):
        result = {
            "Authorization": "Bearer " + token(keys, subject),
            "x-request-id": "agent-runtime-integration",
        }
        if idempotency_key:
            result["Idempotency-Key"] = idempotency_key
        return result

    with TestClient(create_app(settings=auth_settings)) as client:
        created_workspace = client.post(
            "/api/v1/workspaces",
            json={"name": "Runtime workspace"},
            headers=headers(owner),
        )
        assert created_workspace.status_code == 201, created_workspace.text
        workspace_id = created_workspace.json()["id"]
        base = "/api/v1/workspaces/" + workspace_id

        for subject, role in ((admin, "admin"), (member, "member")):
            granted = client.put(
                base + "/members",
                json={"subject": subject, "role": role},
                headers=headers(owner),
            )
            assert granted.status_code == 200, granted.text

        denied_agent = client.post(
            base + "/agents",
            json={"name": "Denied", "instructions": "Do not create"},
            headers=headers(member),
        )
        assert denied_agent.status_code == 403

        created_agent = client.post(
            base + "/agents",
            json={
                "name": "Research agent",
                "instructions": "Summarize the supplied task with citations.",
                "model_profile": "balanced-v1",
            },
            headers=headers(admin),
        )
        assert created_agent.status_code == 201, created_agent.text
        agent_id = created_agent.json()["id"]

        assert client.get(base + "/agents", headers=headers(member)).status_code == 200
        assert client.get(base + "/agents", headers=headers(outsider)).status_code == 404

        run_body = {"agent_id": agent_id, "input": "Prepare the weekly engineering summary."}
        run_headers = headers(member, "weekly-summary-0001")
        created_run = client.post(base + "/runs", json=run_body, headers=run_headers)
        assert created_run.status_code == 201, created_run.text
        run_id = created_run.json()["id"]
        assert created_run.json()["status"] == "queued"

        replay = client.post(base + "/runs", json=run_body, headers=run_headers)
        assert replay.status_code == 200, replay.text
        assert replay.json()["id"] == run_id

        conflict = client.post(
            base + "/runs",
            json={**run_body, "input": "A different task under the same key."},
            headers=run_headers,
        )
        assert conflict.status_code == 409
        assert client.get(base + "/runs/" + run_id, headers=headers(outsider)).status_code == 404

        events = client.get(base + "/runs/" + run_id + "/events", headers=headers(member))
        assert events.status_code == 200, events.text
        assert [event["event_type"] for event in events.json()["items"]] == ["run.queued"]

        other_workspace = client.post(
            "/api/v1/workspaces",
            json={"name": "Other workspace"},
            headers=headers(owner),
        ).json()["id"]
        other_agent = client.post(
            f"/api/v1/workspaces/{other_workspace}/agents",
            json={"name": "Other", "instructions": "Stay isolated."},
            headers=headers(owner),
        ).json()["id"]
        cross_tenant = client.post(
            base + "/runs",
            json={"agent_id": other_agent, "input": "Cross tenant attempt"},
            headers=headers(member, "cross-tenant-0001"),
        )
        assert cross_tenant.status_code == 404

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        outbox_count = connection.execute(
            "SELECT count(*) FROM job_outbox WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT count(*) FROM agent_run_events WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
        assert outbox_count == event_count == 1
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE agent_run_events SET event_type='tampered' WHERE run_id=%s",
                (run_id,),
            )
        connection.rollback()

    async def publish():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            count = await OutboxPublisher(auth_settings).publish_batch(redis)
            entries = await redis.xrange(QUEUE_STREAM, count=1)
            raw = entries[0][1].get(b"job") if entries else None
            return count, json.loads(raw) if raw else None
        finally:
            await redis.aclose()

    published, message = asyncio.run(publish())
    assert published == 1
    assert message["run_id"] == run_id
    assert message["topic"] == "agent.run.queued.v1"
    assert "input" not in message["payload"]

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        row = connection.execute(
            "SELECT published_at,attempts,last_error FROM job_outbox WHERE run_id=%s",
            (run_id,),
        ).fetchone()
        assert row[0] is not None
        assert row[1] == 1
        assert row[2] is None


def test_run_creation_rolls_back_when_outbox_insert_fails(keys, auth_settings):
    migrate(auth_settings)
    owner = "rollback-" + str(uuid4())

    def headers(idempotency_key=None):
        result = {"Authorization": "Bearer " + token(keys, owner)}
        if idempotency_key:
            result["Idempotency-Key"] = idempotency_key
        return result

    with TestClient(create_app(settings=auth_settings)) as client:
        workspace_id = client.post(
            "/api/v1/workspaces",
            json={"name": "Rollback workspace"},
            headers=headers(),
        ).json()["id"]
        base = "/api/v1/workspaces/" + workspace_id
        agent_id = client.post(
            base + "/agents",
            json={"name": "Rollback agent", "instructions": "Test atomicity."},
            headers=headers(),
        ).json()["id"]

        with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
            connection.execute(
                """CREATE OR REPLACE FUNCTION reject_test_outbox() RETURNS trigger
                   LANGUAGE plpgsql AS $$
                   BEGIN RAISE EXCEPTION 'test outbox unavailable'; END; $$"""
            )
            connection.execute(
                """CREATE TRIGGER reject_test_outbox_insert
                   BEFORE INSERT ON job_outbox
                   FOR EACH ROW EXECUTE FUNCTION reject_test_outbox()"""
            )

        response = client.post(
            base + "/runs",
            json={"agent_id": agent_id, "input": "Must roll back"},
            headers=headers("rollback-key-0001"),
        )
        assert response.status_code == 500

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM agent_runs WHERE workspace_id=%s", (workspace_id,)
            ).fetchone()[0]
            == 0
        )
        connection.execute("DROP TRIGGER reject_test_outbox_insert ON job_outbox")
        connection.execute("DROP FUNCTION reject_test_outbox()")


def test_outbox_failures_dead_letter_after_bounded_attempts(keys, auth_settings):
    migrate(auth_settings)
    owner = "dead-letter-" + str(uuid4())

    def headers(idempotency_key=None):
        result = {"Authorization": "Bearer " + token(keys, owner)}
        if idempotency_key:
            result["Idempotency-Key"] = idempotency_key
        return result

    with TestClient(create_app(settings=auth_settings)) as client:
        workspace_id = client.post(
            "/api/v1/workspaces",
            json={"name": "Dead letter workspace"},
            headers=headers(),
        ).json()["id"]
        base = "/api/v1/workspaces/" + workspace_id
        agent_id = client.post(
            base + "/agents",
            json={"name": "Dead letter agent", "instructions": "Test bounded retries."},
            headers=headers(),
        ).json()["id"]
        run_id = client.post(
            base + "/runs",
            json={"agent_id": agent_id, "input": "Fail dispatch"},
            headers=headers("dead-letter-key-0001"),
        ).json()["id"]

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        connection.execute(
            "UPDATE job_outbox SET attempts=9,available_at=now() WHERE run_id=%s",
            (run_id,),
        )

    class FailingRedis:
        async def xadd(self, *_args):
            raise TimeoutError("simulated")

    published = asyncio.run(
        OutboxPublisher(auth_settings).publish_batch(FailingRedis())  # type: ignore[arg-type]
    )
    assert published == 0

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        row = connection.execute(
            """SELECT attempts,published_at,dead_lettered_at,last_error
               FROM job_outbox WHERE run_id=%s""",
            (run_id,),
        ).fetchone()
        assert row[0] == 10
        assert row[1] is None
        assert row[2] is not None
        assert row[3] == "TimeoutError"
