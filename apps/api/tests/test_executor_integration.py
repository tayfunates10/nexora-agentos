import asyncio
import os
from dataclasses import replace
from uuid import UUID

import pytest
from redis.asyncio import Redis
from test_executor import Adapter, response
from test_tool_governance_integration import clear_unpublished_outbox
from test_worker_integration import make_runtime

from nexora_api.execution_fence import executable_run
from nexora_api.executor import DurableAgentExecutor, ExecutionProfile
from nexora_api.executor_store import ExecutorStore
from nexora_api.mcp_gateway import McpGateway
from nexora_api.migrate import migrate
from nexora_api.model_routing import ModelCandidate, ModelCapability, ModelRouter
from nexora_api.outbox import QUEUE_STREAM
from nexora_api.tool_contracts import ToolContractError
from nexora_api.worker import AgentWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]


def test_durable_executor_worker_and_attempt_fence(keys, auth_settings):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    client, headers, workspace_id, run_id = make_runtime(keys, auth_settings, "durable-executor")
    provider = Adapter([response("Persisted answer")])
    store = ExecutorStore(auth_settings)
    executor = DurableAgentExecutor(
        store=store,
        router=ModelRouter([ModelCandidate("test", "test-model", frozenset(ModelCapability))]),
        adapters={"test": provider},
        gateway=McpGateway(auth_settings),
        profiles={
            "default": ExecutionProfile(frozenset({UUID(workspace_id)}), frozenset({"test"}))
        },
    )
    captured = []
    original_check = store.check

    async def check(context):
        captured.append(context)
        async with store.connection() as connection:
            with pytest.raises(ToolContractError, match="execution_fenced"):
                await executable_run(
                    connection, replace(context, attempt_count=context.attempt_count + 1)
                )
        return await original_check(context)

    store.check = check

    async def execute():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = AgentWorker(auth_settings, redis, executor)
            assert await worker.process_once()
            async with store.connection() as connection:
                result = await connection.execute(
                    "SELECT response FROM agent_model_steps WHERE run_id=%s", (run_id,)
                )
                assert (await result.fetchone())["response"]["text"] == "Persisted answer"
            with pytest.raises(ToolContractError, match="execution_fenced"):
                await original_check(captured[0])
        finally:
            await redis.aclose()

    try:
        asyncio.run(execute())
        result = client.get(f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers())
        assert result.json()["status"] == "succeeded", result.text
        assert len(provider.requests) == 1
    finally:
        client.__exit__(None, None, None)
