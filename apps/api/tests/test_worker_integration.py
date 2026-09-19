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
from nexora_api.outbox import QUEUE_STREAM
from nexora_api.run_state import RunStateStore
from nexora_api.worker import AgentWorker, RetryableExecutionError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]


def make_runtime(keys, auth_settings, suffix):
    subject = suffix + "-" + str(uuid4())

    def headers(idempotency_key=None):
        result = {"Authorization": "Bearer " + token(keys, subject)}
        if idempotency_key:
            result["Idempotency-Key"] = idempotency_key
        return result

    client = TestClient(create_app(settings=auth_settings))
    client.__enter__()
    workspace_id = client.post(
        "/api/v1/workspaces", json={"name": suffix}, headers=headers()
    ).json()["id"]
    base = "/api/v1/workspaces/" + workspace_id
    agent_id = client.post(
        base + "/agents",
        json={"name": "Worker agent", "instructions": "Execute through the worker boundary."},
        headers=headers(),
    ).json()["id"]
    run = client.post(
        base + "/runs",
        json={"agent_id": agent_id, "input": "Perform the durable test task."},
        headers=headers(suffix + "-idempotency"),
    )
    assert run.status_code == 201, run.text
    return client, headers, workspace_id, run.json()["id"]


class CountingExecutor:
    def __init__(self):
        self.calls = 0

    async def execute(self, context, is_cancelled):
        self.calls += 1
        assert context.input_text == "Perform the durable test task."
        assert not await is_cancelled()


class RetryOnceExecutor:
    def __init__(self):
        self.calls = 0

    async def execute(self, context, is_cancelled):
        self.calls += 1
        if self.calls == 1:
            raise RetryableExecutionError("provider_unavailable")


def test_worker_success_and_duplicate_delivery(keys, auth_settings):
    migrate(auth_settings)
    client, headers, workspace_id, run_id = make_runtime(keys, auth_settings, "worker-success")
    base = f"/api/v1/workspaces/{workspace_id}"
    executor = CountingExecutor()

    async def exercise():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = AgentWorker(
                auth_settings,
                redis,
                executor,
                worker_id="integration-worker",
                lease_seconds=6,
            )
            assert await worker.process_once(50)
            with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
                row = connection.execute(
                    """SELECT id,workspace_id,run_id,topic,payload
                       FROM job_outbox WHERE run_id=%s ORDER BY created_at LIMIT 1""",
                    (run_id,),
                ).fetchone()
            duplicate = json.dumps(
                {
                    "job_id": str(row[0]),
                    "workspace_id": str(row[1]),
                    "run_id": str(row[2]),
                    "topic": row[3],
                    "payload": row[4],
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            await redis.xadd(QUEUE_STREAM, {"job": duplicate})
            assert await worker.process_once(50)
        finally:
            await redis.aclose()

    asyncio.run(exercise())
    assert executor.calls == 1
    run = client.get(base + "/runs/" + run_id, headers=headers()).json()
    assert run["status"] == "succeeded"
    assert run["attempt_count"] == 1
    events = client.get(base + "/runs/" + run_id + "/events", headers=headers()).json()["items"]
    assert [event["event_type"] for event in events] == [
        "run.queued",
        "run.started",
        "run.succeeded",
    ]
    client.__exit__(None, None, None)


def test_worker_retry_then_success(keys, auth_settings):
    migrate(auth_settings)
    client, headers, workspace_id, run_id = make_runtime(keys, auth_settings, "worker-retry")
    base = f"/api/v1/workspaces/{workspace_id}"
    executor = RetryOnceExecutor()

    async def exercise():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = AgentWorker(
                auth_settings, redis, executor, worker_id="retry-worker", lease_seconds=6
            )
            assert await worker.process_once(50)
            with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
                connection.execute(
                    """UPDATE job_outbox SET available_at=now()
                       WHERE run_id=%s AND published_at IS NULL AND dead_lettered_at IS NULL""",
                    (run_id,),
                )
            assert await worker.process_once(50)
        finally:
            await redis.aclose()

    asyncio.run(exercise())
    assert executor.calls == 2
    run = client.get(base + "/runs/" + run_id, headers=headers()).json()
    assert run["status"] == "succeeded"
    assert run["attempt_count"] == 2
    events = client.get(base + "/runs/" + run_id + "/events", headers=headers()).json()["items"]
    assert [event["event_type"] for event in events] == [
        "run.queued",
        "run.started",
        "run.retry_scheduled",
        "run.started",
        "run.succeeded",
    ]
    client.__exit__(None, None, None)


def test_queued_cancellation_is_idempotent_and_worker_does_not_execute(keys, auth_settings):
    migrate(auth_settings)
    client, headers, workspace_id, run_id = make_runtime(keys, auth_settings, "worker-cancel")
    base = f"/api/v1/workspaces/{workspace_id}"
    first = client.post(base + "/runs/" + run_id + "/cancel", headers=headers())
    second = client.post(base + "/runs/" + run_id + "/cancel", headers=headers())
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "cancelled"
    executor = CountingExecutor()

    async def exercise():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = AgentWorker(auth_settings, redis, executor, lease_seconds=6)
            assert await worker.process_once(50)
        finally:
            await redis.aclose()

    asyncio.run(exercise())
    assert executor.calls == 0
    events = client.get(base + "/runs/" + run_id + "/events", headers=headers()).json()["items"]
    assert [event["event_type"] for event in events] == ["run.queued", "run.cancelled"]
    client.__exit__(None, None, None)


def test_stale_running_lease_is_recovered_to_new_outbox(keys, auth_settings):
    migrate(auth_settings)
    client, headers, workspace_id, run_id = make_runtime(keys, auth_settings, "worker-recovery")
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        job_id = connection.execute(
            "SELECT id FROM job_outbox WHERE run_id=%s ORDER BY created_at LIMIT 1",
            (run_id,),
        ).fetchone()[0]
        connection.execute(
            """UPDATE agent_runs
               SET status='running',attempt_count=1,lease_owner='dead-worker',
                   lease_expires_at=now()-interval '10 seconds'
               WHERE id=%s""",
            (run_id,),
        )
        connection.execute(
            """INSERT INTO worker_job_receipts
               (job_id,workspace_id,run_id,status,worker_id,lease_expires_at)
               VALUES (%s,%s,%s,'processing','dead-worker',now()-interval '10 seconds')""",
            (job_id, workspace_id, run_id),
        )

    recovered = asyncio.run(RunStateStore(auth_settings).recover_stale(orphan_seconds=60))
    assert recovered >= 1
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        run = connection.execute(
            "SELECT status,lease_owner,lease_expires_at FROM agent_runs WHERE id=%s",
            (run_id,),
        ).fetchone()
        pending = connection.execute(
            """SELECT count(*) FROM job_outbox
               WHERE run_id=%s AND published_at IS NULL AND dead_lettered_at IS NULL""",
            (run_id,),
        ).fetchone()[0]
        receipt = connection.execute(
            "SELECT status,last_error_code FROM worker_job_receipts WHERE job_id=%s",
            (job_id,),
        ).fetchone()
    assert run == ("queued", None, None)
    assert pending >= 1
    assert receipt == ("superseded", "lease_expired")
    events = client.get(
        f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/events", headers=headers()
    ).json()["items"]
    assert events[-1]["event_type"] == "run.recovered"
    client.__exit__(None, None, None)
