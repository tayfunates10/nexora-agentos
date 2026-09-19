import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from test_auth import token

from nexora_api.main import create_app
from nexora_api.mcp_gateway import McpGateway
from nexora_api.migrate import migrate
from nexora_api.outbox import QUEUE_STREAM
from nexora_api.worker import AgentWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]


class FakeMcpAdapter:
    def __init__(self):
        self.calls = []

    async def call_tool(self, remote_name, arguments, timeout_seconds):
        self.calls.append((remote_name, arguments, timeout_seconds))
        return {"ok": True, "remote_name": remote_name}


class GatewayExecutor:
    def __init__(self, gateway, tool_name, arguments):
        self.gateway = gateway
        self.tool_name = tool_name
        self.arguments = arguments
        self.calls = 0

    async def execute(self, context, is_cancelled):
        self.calls += 1
        await self.gateway.invoke(
            context,
            "stable-tool-step-1",
            self.tool_name,
            self.arguments,
            is_cancelled,
        )


def auth_headers(keys, subject, idempotency_key=None):
    result = {"Authorization": "Bearer " + token(keys, subject)}
    if idempotency_key:
        result["Idempotency-Key"] = idempotency_key
    return result


def clear_unpublished_outbox(auth_settings):
    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        connection.execute(
            """UPDATE job_outbox SET published_at=now()
               WHERE published_at IS NULL AND dead_lettered_at IS NULL"""
        )


def create_runtime(client, keys, owner, admin, member, suffix):
    workspace = client.post(
        "/api/v1/workspaces",
        json={"name": suffix},
        headers=auth_headers(keys, owner),
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]
    base = f"/api/v1/workspaces/{workspace_id}"

    for subject, role in ((admin, "admin"), (member, "member")):
        response = client.put(
            base + "/members",
            json={"subject": subject, "role": role},
            headers=auth_headers(keys, owner),
        )
        assert response.status_code == 200, response.text

    agent = client.post(
        base + "/agents",
        json={"name": "Tool agent", "instructions": "Use only governed tools."},
        headers=auth_headers(keys, admin),
    )
    assert agent.status_code == 201, agent.text
    return workspace_id, base, agent.json()["id"]


def register_tool(client, keys, base, admin, name, side_effect):
    properties = {"query": {"type": "string", "minLength": 1, "maxLength": 100}}
    required = ["query"]
    if side_effect != "read":
        properties["idempotency_key"] = {
            "type": "string",
            "minLength": 8,
            "maxLength": 128,
        }
        required.append("idempotency_key")

    response = client.put(
        base + "/tools/" + name,
        json={
            "server_key": "integration",
            "remote_name": "remote_" + name,
            "description": "Integration contract for " + name,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "remote_name": {"type": "string"},
                },
                "required": ["ok", "remote_name"],
                "additionalProperties": False,
            },
            "side_effect": side_effect,
            "enabled": True,
        },
        headers=auth_headers(keys, admin),
    )
    assert response.status_code == 200, response.text
    policy = client.put(
        base + "/tools/" + name + "/policy",
        json={"decision": "allow", "reason": "Allowed by integration policy."},
        headers=auth_headers(keys, admin),
    )
    assert policy.status_code == 200, policy.text


def create_run(client, keys, base, member, agent_id, suffix):
    response = client.post(
        base + "/runs",
        json={"agent_id": agent_id, "input": "Execute the governed tool step."},
        headers=auth_headers(keys, member, suffix + "-run-key"),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def process_once(auth_settings, executor, worker_id):
    async def exercise():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            worker = AgentWorker(
                auth_settings,
                redis,
                executor,
                worker_id=worker_id,
                lease_seconds=6,
            )
            return await worker.process_once(50)
        finally:
            await redis.aclose()

    return asyncio.run(exercise())


def test_allowed_read_tool_executes_through_gateway(keys, auth_settings):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    prefix = "tool-read-" + str(uuid4())
    owner, admin, member = (prefix + suffix for suffix in ("-owner", "-admin", "-member"))
    adapter = FakeMcpAdapter()

    with TestClient(create_app(settings=auth_settings)) as client:
        _workspace_id, base, agent_id = create_runtime(
            client, keys, owner, admin, member, "Read tool workspace"
        )
        denied = client.put(
            base + "/tools/member-tool",
            json={
                "server_key": "integration",
                "remote_name": "denied",
                "description": "Must not be registered by member.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                "side_effect": "read",
            },
            headers=auth_headers(keys, member),
        )
        assert denied.status_code == 403

        register_tool(client, keys, base, admin, "lookup", "read")
        run_id = create_run(client, keys, base, member, agent_id, "tool-read")

        async def clear_stream():
            redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
            try:
                await redis.delete(QUEUE_STREAM)
            finally:
                await redis.aclose()

        asyncio.run(clear_stream())
        gateway = McpGateway(auth_settings, {"integration": adapter})
        executor = GatewayExecutor(gateway, "lookup", {"query": "service status"})
        assert process_once(auth_settings, executor, "tool-read-worker")

        run = client.get(base + "/runs/" + run_id, headers=auth_headers(keys, member))
        assert run.status_code == 200
        assert run.json()["status"] == "succeeded"
        assert executor.calls == 1
        assert len(adapter.calls) == 1
        assert adapter.calls[0][0] == "remote_lookup"

        events = client.get(
            base + "/runs/" + run_id + "/events", headers=auth_headers(keys, member)
        ).json()["items"]
        event_types = [event["event_type"] for event in events]
        assert "tool.call_planned" in event_types
        assert "tool.started" in event_types
        assert "tool.succeeded" in event_types

        with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
            row = connection.execute(
                """SELECT status,attempt_count,result
                   FROM tool_calls WHERE run_id=%s""",
                (run_id,),
            ).fetchone()
        assert row[0] == "succeeded"
        assert row[1] == 1
        assert row[2]["ok"] is True


def test_destructive_tool_requires_durable_approval_and_resumes(keys, auth_settings):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    prefix = "tool-approval-" + str(uuid4())
    owner, admin, member, outsider = (
        prefix + suffix for suffix in ("-owner", "-admin", "-member", "-outsider")
    )
    adapter = FakeMcpAdapter()

    with TestClient(create_app(settings=auth_settings)) as client:
        _workspace_id, base, agent_id = create_runtime(
            client, keys, owner, admin, member, "Approval workspace"
        )
        register_tool(client, keys, base, admin, "dangerous-delete", "destructive")
        run_id = create_run(client, keys, base, member, agent_id, "tool-approval")

        async def clear_stream():
            redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
            try:
                await redis.delete(QUEUE_STREAM)
            finally:
                await redis.aclose()

        asyncio.run(clear_stream())
        gateway = McpGateway(auth_settings, {"integration": adapter})
        arguments = {
            "query": "delete archived record",
            "idempotency_key": "delete-archive-0001",
        }
        executor = GatewayExecutor(gateway, "dangerous-delete", arguments)
        assert process_once(auth_settings, executor, "approval-worker")

        paused = client.get(base + "/runs/" + run_id, headers=auth_headers(keys, member))
        assert paused.json()["status"] == "waiting_for_approval"
        assert adapter.calls == []

        member_list = client.get(base + "/approvals", headers=auth_headers(keys, member))
        assert member_list.status_code == 403
        outsider_list = client.get(base + "/approvals", headers=auth_headers(keys, outsider))
        assert outsider_list.status_code == 404

        approvals = client.get(base + "/approvals", headers=auth_headers(keys, admin))
        assert approvals.status_code == 200, approvals.text
        pending = [item for item in approvals.json()["items"] if item["run_id"] == run_id]
        assert len(pending) == 1
        approval = pending[0]
        assert approval["status"] == "pending"
        assert approval["normalized_arguments"] == arguments

        decided = client.post(
            base + f"/approvals/{approval['id']}/decision",
            json={"decision": "approved"},
            headers=auth_headers(keys, admin),
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "approved"

        queued = client.get(base + "/runs/" + run_id, headers=auth_headers(keys, member))
        assert queued.json()["status"] == "queued"
        assert process_once(auth_settings, executor, "approval-worker")

        completed = client.get(base + "/runs/" + run_id, headers=auth_headers(keys, member))
        assert completed.json()["status"] == "succeeded"
        assert executor.calls == 2
        assert len(adapter.calls) == 1
        assert adapter.calls[0][1]["idempotency_key"] == "delete-archive-0001"

        events = client.get(
            base + "/runs/" + run_id + "/events", headers=auth_headers(keys, member)
        ).json()["items"]
        event_types = [event["event_type"] for event in events]
        assert "tool.approval_requested" in event_types
        assert "run.waiting_for_approval" in event_types
        assert "tool.approved" in event_types
        assert "run.resumed" in event_types
        assert "tool.succeeded" in event_types

        with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
            with pytest.raises(psycopg.errors.RaiseException):
                connection.execute(
                    """UPDATE tool_approvals
                       SET normalized_arguments='{}'::jsonb WHERE id=%s""",
                    (approval["id"],),
                )
            connection.rollback()



def test_contract_change_invalidates_pending_approval(keys, auth_settings):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    prefix = "tool-contract-change-" + str(uuid4())
    owner, admin, member = (
        prefix + suffix for suffix in ("-owner", "-admin", "-member")
    )
    adapter = FakeMcpAdapter()

    with TestClient(create_app(settings=auth_settings)) as client:
        _workspace_id, base, agent_id = create_runtime(
            client, keys, owner, admin, member, "Contract change workspace"
        )
        register_tool(client, keys, base, admin, "dangerous-change", "destructive")
        run_id = create_run(client, keys, base, member, agent_id, "tool-contract-change")

        async def clear_stream():
            redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
            try:
                await redis.delete(QUEUE_STREAM)
            finally:
                await redis.aclose()

        asyncio.run(clear_stream())
        gateway = McpGateway(auth_settings, {"integration": adapter})
        arguments = {
            "query": "change protected record",
            "idempotency_key": "contract-change-0001",
        }
        executor = GatewayExecutor(gateway, "dangerous-change", arguments)
        assert process_once(auth_settings, executor, "contract-change-worker")

        approvals = client.get(base + "/approvals", headers=auth_headers(keys, admin))
        pending = [item for item in approvals.json()["items"] if item["run_id"] == run_id]
        assert len(pending) == 1
        approval_id = pending[0]["id"]

        changed = client.put(
            base + "/tools/dangerous-change",
            json={
                "server_key": "integration",
                "remote_name": "different_remote_action",
                "description": "Changed after approval request.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 100},
                        "idempotency_key": {
                            "type": "string",
                            "minLength": 8,
                            "maxLength": 128,
                        },
                    },
                    "required": ["query", "idempotency_key"],
                    "additionalProperties": False,
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "remote_name": {"type": "string"},
                    },
                    "required": ["ok", "remote_name"],
                    "additionalProperties": False,
                },
                "side_effect": "destructive",
                "enabled": True,
            },
            headers=auth_headers(keys, admin),
        )
        assert changed.status_code == 200, changed.text

        decided = client.post(
            base + f"/approvals/{approval_id}/decision",
            json={"decision": "approved"},
            headers=auth_headers(keys, admin),
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "cancelled"

        run = client.get(base + "/runs/" + run_id, headers=auth_headers(keys, member))
        assert run.json()["status"] == "cancelled"
        assert adapter.calls == []
