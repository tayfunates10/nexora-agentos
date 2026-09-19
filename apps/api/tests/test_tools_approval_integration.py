import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from test_auth import token

from nexora_api.main import create_app
from nexora_api.mcp_gateway import canonical_payload
from nexora_api.migrate import migrate
from nexora_api.outbox import QUEUE_STREAM
from nexora_api.run_state import RunStateStore, WorkerJob
from nexora_api.tool_registry import SideEffect, ToolRegistry, ToolSpec
from nexora_api.worker import AgentWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]


class WriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    value: str = Field(min_length=1, max_length=100)


class WriteOutput(BaseModel):
    value: str
    execution_count: int


def registry_with_counter(counter):
    registry = ToolRegistry()

    async def write(_context, arguments):
        body = WriteInput.model_validate(arguments)
        counter["calls"] += 1
        return WriteOutput(value=body.value, execution_count=counter["calls"])

    registry.register(
        ToolSpec(
            name="test.write",
            description="Idempotent test mutation used only by integration tests.",
            input_model=WriteInput,
            output_model=WriteOutput,
            side_effect=SideEffect.WRITE,
            handler=write,
        )
    )
    return registry


def setup_workspace(keys, settings, registry, prefix):
    owner = prefix + "-owner-" + str(uuid4())
    admin = prefix + "-admin-" + str(uuid4())
    member = prefix + "-member-" + str(uuid4())
    outsider = prefix + "-outsider-" + str(uuid4())

    def headers(subject):
        return {"Authorization": "Bearer " + token(keys, subject)}

    client = TestClient(create_app(settings=settings, tool_registry=registry))
    client.__enter__()
    workspace = client.post("/api/v1/workspaces", json={"name": prefix}, headers=headers(owner))
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]
    base = "/api/v1/workspaces/" + workspace_id
    for subject, role in ((admin, "admin"), (member, "member")):
        response = client.put(
            base + "/members",
            json={"subject": subject, "role": role},
            headers=headers(owner),
        )
        assert response.status_code == 200, response.text
    agent = client.post(
        base + "/agents",
        json={"name": "Tool agent", "instructions": "Use typed tools only."},
        headers=headers(owner),
    )
    assert agent.status_code == 201, agent.text
    return client, headers, workspace_id, agent.json()["id"], owner, admin, member, outsider


def create_run(client, headers, workspace_id, agent_id, member, suffix):
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/runs",
        json={"agent_id": agent_id, "input": "Perform a controlled tool action."},
        headers={**headers(member), "Idempotency-Key": suffix + "-run-key"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


class GatewayExecutor:
    def __init__(self, gateway, counter, value="approved-value"):
        self.gateway = gateway
        self.counter = counter
        self.value = value
        self.completed_calls = 0

    async def execute(self, context, _is_cancelled):
        outcome = await self.gateway.call_tool(
            context,
            "test.write",
            {"value": self.value},
            "tool-call-key-0001",
        )
        assert outcome.status == "succeeded"
        replay = await self.gateway.call_tool(
            context,
            "test.write",
            {"value": self.value},
            "tool-call-key-0001",
        )
        assert replay.tool_call_id == outcome.tool_call_id
        assert replay.output == outcome.output
        self.completed_calls += 1


def test_worker_pauses_for_approval_then_resumes_once(keys, auth_settings):
    migrate(auth_settings)
    counter = {"calls": 0}
    registry = registry_with_counter(counter)
    client, headers, workspace_id, agent_id, owner, _admin, member, outsider = setup_workspace(
        keys, auth_settings, registry, "approval-flow"
    )
    run_id = create_run(client, headers, workspace_id, agent_id, member, "approval-flow")
    base = f"/api/v1/workspaces/{workspace_id}"
    executor = GatewayExecutor(client.app.state.tool_gateway, counter)

    async def first_worker():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = AgentWorker(
                auth_settings,
                redis,
                executor,
                worker_id="approval-worker",
                lease_seconds=6,
            )
            assert await worker.process_once(50)
        finally:
            await redis.aclose()

    asyncio.run(first_worker())
    assert counter["calls"] == 0
    waiting = client.get(base + "/runs/" + run_id, headers=headers(member)).json()
    assert waiting["status"] == "waiting_for_approval"

    assert client.get(base + "/approvals", headers=headers(member)).status_code == 403
    assert client.get(base + "/approvals", headers=headers(outsider)).status_code == 404
    pending = client.get(base + "/approvals", headers=headers(owner))
    assert pending.status_code == 200, pending.text
    approval = pending.json()["items"][0]
    assert approval["arguments"] == {"value": "approved-value"}

    approved = client.post(
        base + "/approvals/" + approval["id"],
        json={"decision": "approve", "reason": "Reviewed by workspace owner"},
        headers=headers(owner),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert (
        client.get(base + "/runs/" + run_id, headers=headers(member)).json()["status"] == "queued"
    )

    async def second_worker():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            worker = AgentWorker(
                auth_settings,
                redis,
                executor,
                worker_id="resume-worker",
                lease_seconds=6,
            )
            assert await worker.process_once(50)
        finally:
            await redis.aclose()

    asyncio.run(second_worker())
    assert counter["calls"] == 1
    assert executor.completed_calls == 1
    final = client.get(base + "/runs/" + run_id, headers=headers(member)).json()
    assert final["status"] == "succeeded"

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        call = connection.execute(
            """SELECT status,arguments_hash,result,error_code,duration_ms
               FROM tool_calls WHERE run_id=%s""",
            (run_id,),
        ).fetchone()
        assert call[0] == "succeeded"
        assert len(call[1]) == 64
        assert call[2]["execution_count"] == 1
        assert call[3] is None
        assert call[4] is not None
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                """UPDATE tool_calls
                   SET arguments='{"value":"tampered"}'::jsonb WHERE run_id=%s""",
                (run_id,),
            )
        connection.rollback()

    events = client.get(base + "/runs/" + run_id + "/events", headers=headers(member)).json()[
        "items"
    ]
    names = [event["event_type"] for event in events]
    assert "tool.approval_requested" in names
    assert "tool.approval_approved" in names
    assert "tool.execution_started" in names
    assert "tool.succeeded" in names
    client.__exit__(None, None, None)


def test_only_owner_changes_policy_and_allow_skips_approval(keys, auth_settings):
    migrate(auth_settings)
    counter = {"calls": 0}
    registry = registry_with_counter(counter)
    client, headers, workspace_id, agent_id, owner, admin, member, outsider = setup_workspace(
        keys, auth_settings, registry, "policy-flow"
    )
    base = f"/api/v1/workspaces/{workspace_id}"

    denied = client.put(
        base + "/tools/test.write/policy",
        json={"decision": "allow"},
        headers=headers(admin),
    )
    assert denied.status_code == 403
    allowed = client.put(
        base + "/tools/test.write/policy",
        json={"decision": "allow"},
        headers=headers(owner),
    )
    assert allowed.status_code == 200, allowed.text
    tools = client.get(base + "/tools", headers=headers(member))
    assert tools.status_code == 200
    assert tools.json()[0]["effective_policy"] == "allow"
    assert client.get(base + "/tools", headers=headers(outsider)).status_code == 404

    run_id = create_run(client, headers, workspace_id, agent_id, member, "policy-flow")
    executor = GatewayExecutor(client.app.state.tool_gateway, counter, value="allowed-value")

    async def execute():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = AgentWorker(
                auth_settings, redis, executor, worker_id="policy-worker", lease_seconds=6
            )
            assert await worker.process_once(50)
        finally:
            await redis.aclose()

    asyncio.run(execute())
    assert counter["calls"] == 1
    assert client.get(base + "/approvals", headers=headers(owner)).json()["items"] == []
    assert (
        client.get(base + "/runs/" + run_id, headers=headers(member)).json()["status"]
        == "succeeded"
    )
    client.__exit__(None, None, None)


def test_requester_can_cancel_pending_approval_and_run(keys, auth_settings):
    migrate(auth_settings)
    counter = {"calls": 0}
    registry = registry_with_counter(counter)
    client, headers, workspace_id, agent_id, owner, _admin, member, _outsider = setup_workspace(
        keys, auth_settings, registry, "cancel-flow"
    )
    run_id = create_run(client, headers, workspace_id, agent_id, member, "cancel-flow")
    base = f"/api/v1/workspaces/{workspace_id}"
    executor = GatewayExecutor(client.app.state.tool_gateway, counter)

    async def pause():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = AgentWorker(
                auth_settings, redis, executor, worker_id="cancel-worker", lease_seconds=6
            )
            assert await worker.process_once(50)
        finally:
            await redis.aclose()

    asyncio.run(pause())
    approval = client.get(base + "/approvals", headers=headers(owner)).json()["items"][0]
    cancelled = client.post(
        base + "/approvals/" + approval["id"],
        json={"decision": "cancel", "reason": "Requester changed intent"},
        headers=headers(member),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert (
        client.get(base + "/runs/" + run_id, headers=headers(member)).json()["status"] == "queued"
    )

    # A new pending approval is also cancelled atomically when the whole run is cancelled.
    second_run = create_run(client, headers, workspace_id, agent_id, member, "cancel-whole-run")

    async def pause_second():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            second_executor = GatewayExecutor(
                client.app.state.tool_gateway, counter, value="second-value"
            )
            worker = AgentWorker(
                auth_settings,
                redis,
                second_executor,
                worker_id="cancel-run-worker",
                lease_seconds=6,
            )
            assert await worker.process_once(50)
        finally:
            await redis.aclose()

    asyncio.run(pause_second())
    response = client.post(base + "/runs/" + second_run + "/cancel", headers=headers(member))
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        statuses = connection.execute(
            """SELECT c.status,a.status FROM tool_calls c
               JOIN tool_approvals a ON a.tool_call_id=c.id WHERE c.run_id=%s""",
            (second_run,),
        ).fetchone()
    assert statuses == ("cancelled", "cancelled")
    client.__exit__(None, None, None)


def test_pending_approval_expires_and_run_resumes(keys, auth_settings):
    short_settings = auth_settings.model_copy(update={"tool_approval_ttl_seconds": 1})
    migrate(short_settings)
    counter = {"calls": 0}
    registry = registry_with_counter(counter)
    client, headers, workspace_id, agent_id, owner, _admin, member, _outsider = setup_workspace(
        keys, short_settings, registry, "expiry-flow"
    )
    run_id = create_run(client, headers, workspace_id, agent_id, member, "expiry-flow")
    base = f"/api/v1/workspaces/{workspace_id}"
    executor = GatewayExecutor(client.app.state.tool_gateway, counter)

    async def pause():
        redis = Redis.from_url(short_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = AgentWorker(
                short_settings, redis, executor, worker_id="expiry-worker", lease_seconds=6
            )
            assert await worker.process_once(50)
        finally:
            await redis.aclose()

    asyncio.run(pause())
    asyncio.run(asyncio.sleep(1.1))
    assert asyncio.run(client.app.state.approvals.expire_pending()) >= 1
    assert client.get(base + "/approvals", headers=headers(owner)).json()["items"] == []
    assert (
        client.get(base + "/runs/" + run_id, headers=headers(member)).json()["status"] == "queued"
    )
    with psycopg.connect(short_settings.database_url.get_secret_value()) as connection:
        status = connection.execute(
            """SELECT c.status,a.status FROM tool_calls c
               JOIN tool_approvals a ON a.tool_call_id=c.id WHERE c.run_id=%s""",
            (run_id,),
        ).fetchone()
    assert status == ("expired", "expired")
    assert counter["calls"] == 0
    client.__exit__(None, None, None)


def test_approved_tool_replay_after_worker_crash_reuses_approval(keys, auth_settings):
    migrate(auth_settings)
    counter = {"calls": 0}
    registry = registry_with_counter(counter)
    client, headers, workspace_id, agent_id, owner, _admin, member, _outsider = setup_workspace(
        keys, auth_settings, registry, "approved-replay"
    )
    run_id = create_run(client, headers, workspace_id, agent_id, member, "approved-replay")

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        outbox = connection.execute(
            """SELECT id,workspace_id,run_id,topic,payload
               FROM job_outbox WHERE run_id=%s ORDER BY created_at LIMIT 1""",
            (run_id,),
        ).fetchone()

    job = WorkerJob.model_validate(
        {
            "job_id": outbox[0],
            "workspace_id": outbox[1],
            "run_id": outbox[2],
            "topic": outbox[3],
            "payload": outbox[4],
        }
    )
    state = RunStateStore(auth_settings)
    claim = asyncio.run(state.claim(job, "crash-replay-worker", lease_seconds=30))
    assert claim.action == "execute"
    assert claim.context is not None
    context = claim.context

    arguments, arguments_hash = canonical_payload(WriteInput(value="crash-value"))
    tool_call_id = uuid4()
    approval_id = uuid4()
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        connection.execute(
            """INSERT INTO tool_calls
               (id,workspace_id,run_id,tool_name,schema_version,side_effect,
                arguments,arguments_hash,idempotency_key,requested_by_issuer,
                requested_by_subject,policy_decision,policy_reason,status,started_at)
               VALUES (%s,%s,%s,'test.write',1,'write',%s,%s,'tool-call-key-0001',
                       %s,%s,'require_approval','default_mutation_approval',
                       'executing',now())""",
            (
                tool_call_id,
                workspace_id,
                run_id,
                psycopg.types.json.Jsonb(arguments),
                arguments_hash,
                auth_settings.auth_issuer,
                member,
            ),
        )
        connection.execute(
            """INSERT INTO tool_approvals
               (id,workspace_id,run_id,tool_call_id,arguments_hash,
                requested_by_issuer,requested_by_subject,status,policy_reason,
                expires_at,decided_by_issuer,decided_by_subject,decided_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,'approved',
                       'default_mutation_approval',now()+interval '1 day',
                       %s,%s,now())""",
            (
                approval_id,
                workspace_id,
                run_id,
                tool_call_id,
                arguments_hash,
                auth_settings.auth_issuer,
                member,
                auth_settings.auth_issuer,
                owner,
            ),
        )

    outcome = asyncio.run(
        client.app.state.tool_gateway.call_tool(
            context,
            "test.write",
            {"value": "crash-value"},
            "tool-call-key-0001",
        )
    )
    assert outcome.status == "succeeded"
    assert outcome.tool_call_id == tool_call_id
    assert counter["calls"] == 1

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        count = connection.execute(
            "SELECT count(*) FROM tool_approvals WHERE tool_call_id=%s",
            (tool_call_id,),
        ).fetchone()[0]
    assert count == 1
    assert asyncio.run(state.complete_success(job, "crash-replay-worker"))
    client.__exit__(None, None, None)
