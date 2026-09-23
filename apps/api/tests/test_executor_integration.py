import asyncio
import os
from dataclasses import replace
from uuid import UUID, uuid4

import psycopg
import pytest
from redis.asyncio import Redis
from test_auth import token
from test_executor import Adapter, response
from test_tool_governance_integration import clear_unpublished_outbox
from test_worker_integration import make_runtime

from nexora_api.answer_cache import SemanticAnswerCache, SemanticAnswerCacheConfig
from nexora_api.embeddings import EmbeddingBatch
from nexora_api.execution_fence import executable_run
from nexora_api.executor import DurableAgentExecutor, ExecutionProfile
from nexora_api.executor_store import ExecutorStore
from nexora_api.mcp_gateway import McpGateway
from nexora_api.migrate import migrate
from nexora_api.model_routing import ModelCandidate, ModelCapability, ModelRouter
from nexora_api.outbox import QUEUE_STREAM
from nexora_api.spend import EmbeddingSpend, ModelPrice, answer_cache_embedding_source_key
from nexora_api.tool_contracts import ToolContractError
from nexora_api.worker import AgentWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]


class SemanticEmbeddingAdapter:
    name = "test-embeddings"

    def __init__(self):
        self.calls = []

    async def embed(self, *, request_id, texts, model, dimensions, timeout_seconds):
        self.calls.append((request_id, texts, model, dimensions, timeout_seconds))
        vectors = {
            "perform the durable test task.": (1.0, 0.0, 0.0),
            "complete the same durable task for me.": (0.99, 0.1, 0.0),
        }
        return EmbeddingBatch(
            vectors=tuple(vectors[text] for text in texts),
            model=model,
            dimensions=dimensions,
            input_tokens=4,
        )

    async def cancel(self, request_id):
        return None


def test_workspace_answer_cache_reuses_answer_without_second_provider_call(keys, auth_settings):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    client, headers, workspace_id, first_run_id = make_runtime(keys, auth_settings, "answer-cache")
    base = f"/api/v1/workspaces/{workspace_id}"
    first_run = client.get(base + f"/runs/{first_run_id}", headers=headers()).json()
    second = client.post(
        base + "/runs",
        json={
            "agent_id": first_run["agent_id"],
            "input": "  PERFORM   THE DURABLE TEST TASK.  ",
        },
        headers=headers("answer-cache-second"),
    )
    assert second.status_code == 201, second.text
    second_run_id = second.json()["id"]

    provider = Adapter([response("Reusable workspace answer")])
    executor = DurableAgentExecutor(
        store=ExecutorStore(auth_settings),
        router=ModelRouter([ModelCandidate("test", "test-model", frozenset(ModelCapability))]),
        adapters={"test": provider},
        gateway=McpGateway(auth_settings),
        profiles={
            "default": ExecutionProfile(
                frozenset({UUID(workspace_id)}),
                frozenset({"test"}),
                answer_cache_ttl_seconds=3600,
            )
        },
    )

    async def execute():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = AgentWorker(auth_settings, redis, executor)
            assert await worker.process_once()
            assert await worker.process_once()
        finally:
            await redis.aclose()

    try:
        asyncio.run(execute())
        assert len(provider.requests) == 1

        first_result = client.get(base + f"/runs/{first_run_id}/result", headers=headers()).json()
        second_result = client.get(base + f"/runs/{second_run_id}/result", headers=headers()).json()
        assert first_result["output_text"] == "Reusable workspace answer"
        assert first_result["recorded_input_tokens"] == 10
        assert first_result["recorded_output_tokens"] == 1
        assert second_result["output_text"] == "Reusable workspace answer"
        assert second_result["recorded_input_tokens"] == 0
        assert second_result["recorded_output_tokens"] == 0

        with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
            cache = connection.execute(
                """SELECT workspace_id,agent_id,hit_count,response
                   FROM workspace_answer_cache
                   WHERE workspace_id=%s""",
                (workspace_id,),
            ).fetchone()
            assert str(cache[0]) == workspace_id
            assert str(cache[1]) == first_run["agent_id"]
            assert cache[2] == 1
            assert cache[3]["usage"] == {"input_tokens": 0, "output_tokens": 0}
            cached_step = connection.execute(
                """SELECT routing_reason,response
                   FROM agent_model_steps WHERE run_id=%s AND step_no=0""",
                (second_run_id,),
            ).fetchone()
            assert cached_step[0] == "workspace_answer_cache"
            assert cached_step[1]["usage"] == {
                "input_tokens": 0,
                "output_tokens": 0,
            }
    finally:
        client.__exit__(None, None, None)


def test_semantic_answer_cache_reuses_paraphrase_with_pgvector(keys, auth_settings):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    client, headers, workspace_id, first_run_id = make_runtime(
        keys, auth_settings, "semantic-answer-cache"
    )
    base = f"/api/v1/workspaces/{workspace_id}"
    first_run = client.get(base + f"/runs/{first_run_id}", headers=headers()).json()
    second = client.post(
        base + "/runs",
        json={
            "agent_id": first_run["agent_id"],
            "input": "Complete the same durable task for me.",
        },
        headers=headers("semantic-answer-cache-second"),
    )
    assert second.status_code == 201, second.text
    second_run_id = second.json()["id"]

    provider = Adapter([response("Semantic reusable answer")])
    embedding_adapter = SemanticEmbeddingAdapter()
    semantic_cache = SemanticAnswerCache(
        embedding_adapter,
        SemanticAnswerCacheConfig(
            model="semantic-test-model",
            dimensions=3,
            similarity_threshold=0.94,
            timeout_seconds=5,
            spend=EmbeddingSpend(
                provider="test-embeddings",
                model="semantic-test-model",
                price=ModelPrice(
                    input_micros_per_million_tokens=1_000_000,
                    output_micros_per_million_tokens=0,
                ),
            ),
        ),
    )
    executor = DurableAgentExecutor(
        store=ExecutorStore(auth_settings),
        router=ModelRouter([ModelCandidate("test", "test-model", frozenset(ModelCapability))]),
        adapters={"test": provider},
        gateway=McpGateway(auth_settings),
        profiles={
            "default": ExecutionProfile(
                frozenset({UUID(workspace_id)}),
                frozenset({"test"}),
                answer_cache_ttl_seconds=3600,
            )
        },
        semantic_cache=semantic_cache,
    )

    async def execute():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = AgentWorker(auth_settings, redis, executor)
            assert await worker.process_once()
            assert await worker.process_once()
        finally:
            await redis.aclose()

    try:
        asyncio.run(execute())
        assert len(provider.requests) == 1
        assert len(embedding_adapter.calls) == 2

        second_result = client.get(base + f"/runs/{second_run_id}/result", headers=headers()).json()
        assert second_result["output_text"] == "Semantic reusable answer"
        assert second_result["recorded_input_tokens"] == 0
        assert second_result["recorded_output_tokens"] == 0

        with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
            cache = connection.execute(
                """SELECT scope_key,normalized_question,embedding_model,
                          embedding_dimensions,embedding IS NOT NULL,hit_count
                   FROM workspace_answer_cache
                   WHERE workspace_id=%s""",
                (workspace_id,),
            ).fetchone()
            assert cache[0] is not None
            assert cache[1] == "perform the durable test task."
            assert cache[2] == "semantic-test-model"
            assert cache[3] == 3
            assert cache[4] is True
            assert cache[5] == 1

            cached_step = connection.execute(
                """SELECT routing_reason,response
                   FROM agent_model_steps WHERE run_id=%s AND step_no=0""",
                (second_run_id,),
            ).fetchone()
            assert cached_step[0] == "workspace_semantic_answer_cache"
            assert cached_step[1]["usage"] == {
                "input_tokens": 0,
                "output_tokens": 0,
            }
            spends = connection.execute(
                """SELECT source_key,category,provider,model,input_tokens,cost_micros
                   FROM workspace_spend_records
                   WHERE workspace_id=%s AND category='embedding'
                   ORDER BY occurred_at""",
                (workspace_id,),
            ).fetchall()
            assert [row[0] for row in spends] == [
                answer_cache_embedding_source_key(UUID(first_run_id)),
                answer_cache_embedding_source_key(UUID(second_run_id)),
            ]
            assert all(row[1] == "embedding" for row in spends)
            assert all(row[2] == "test-embeddings" for row in spends)
            assert all(row[3] == "semantic-test-model" for row in spends)
            assert all(row[4] == 4 for row in spends)
            assert all(row[5] == 4 for row in spends)
    finally:
        client.__exit__(None, None, None)


def test_durable_executor_worker_and_attempt_fence(keys, auth_settings):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    client, headers, workspace_id, run_id = make_runtime(keys, auth_settings, "durable-executor")
    base = f"/api/v1/workspaces/{workspace_id}"
    result_path = base + f"/runs/{run_id}/result"
    assert client.get(result_path, headers=headers()).status_code == 409
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
        saved = client.get(result_path, headers=headers())
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        assert saved.json()["output_text"] == "Persisted answer"
        assert saved.json()["finish_reason"] == "stop"
        assert saved.json()["recorded_input_tokens"] == 10
        assert saved.json()["recorded_output_tokens"] == 1
        assert saved.json()["model_steps"][0]["model"] == "test-model"
        assert "response" not in saved.json()["model_steps"][0]
        assert client.get(result_path).status_code == 401
        assert client.get(base + f"/runs/{uuid4()}/result", headers=headers()).status_code == 404
        assert (
            client.get(
                f"/api/v1/workspaces/{uuid4()}/runs/{run_id}/result", headers=headers()
            ).status_code
            == 404
        )

        admin, member = str(uuid4()), str(uuid4())
        for subject, role in ((admin, "admin"), (member, "member")):
            granted = client.put(
                base + "/members", json={"subject": subject, "role": role}, headers=headers()
            )
            assert granted.status_code == 200
            other_headers = {"Authorization": "Bearer " + token(keys, subject)}
            assert client.get(result_path, headers=other_headers).status_code == 404

        member_headers = {"Authorization": "Bearer " + token(keys, member)}
        created = client.post(
            base + "/runs",
            json={"agent_id": result.json()["agent_id"], "input": "Cancel before execution."},
            headers={**member_headers, "Idempotency-Key": str(uuid4())},
        )
        assert created.status_code == 201, created.text
        member_run_id = created.json()["id"]
        cancelled = client.post(base + f"/runs/{member_run_id}/cancel", headers=member_headers)
        assert cancelled.status_code == 200
        member_result_path = base + f"/runs/{member_run_id}/result"
        member_result = client.get(member_result_path, headers=member_headers)
        assert member_result.status_code == 200, member_result.text
        assert member_result.json()["status"] == "cancelled"
        assert member_result.json()["output_text"] is None
        assert member_result.json()["model_steps"] == []
        assert client.get(member_result_path, headers=headers()).status_code == 404
        with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
            connection.execute(
                "DELETE FROM workspace_memberships WHERE workspace_id=%s AND subject=%s",
                (workspace_id, member),
            )
        assert client.get(member_result_path, headers=member_headers).status_code == 404
    finally:
        client.__exit__(None, None, None)
