import asyncio
import os
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from test_auth import token
from test_executor import Adapter, response
from test_knowledge_ingestion_integration import StubEmbeddingAdapter, setup_workspace
from test_tool_governance_integration import clear_unpublished_outbox
from test_worker_integration import make_runtime

from nexora_api import metrics
from nexora_api.auth import Principal
from nexora_api.evaluation_judge_worker import EvaluationJudgeWorker, JudgeExecutionError
from nexora_api.executor import DurableAgentExecutor, ExecutionProfile
from nexora_api.executor_store import ExecutorStore
from nexora_api.knowledge_worker import KnowledgeIngestionWorker
from nexora_api.main import create_app
from nexora_api.mcp_gateway import McpGateway
from nexora_api.migrate import migrate
from nexora_api.model_routing import ModelCandidate, ModelCapability, ModelRouter
from nexora_api.outbox import QUEUE_STREAM
from nexora_api.rag_pipeline import RagEmbeddingPipeline
from nexora_api.rag_repository import RagRepository
from nexora_api.runtime_config import EvaluationJudgeConfig
from nexora_api.spend import (
    EmbeddingSpend,
    ModelPrice,
    SpendPolicy,
    SpendPricingError,
    agent_step_source_key,
    knowledge_source_key,
    run_retrieval_source_key,
)
from nexora_api.spend_repository import record_spend
from nexora_api.worker import AgentWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]

# One micro per input token, two per output token.
PRICE = ModelPrice(
    input_micros_per_million_tokens=1_000_000,
    output_micros_per_million_tokens=2_000_000,
)


class NamedAdapter(Adapter):
    """The judge worker refuses an adapter that is not the configured provider."""

    name = "test"


def policy(provider="test", model="test-model"):
    return SpendPolicy(prices={(provider, model): PRICE})


def seed_record(settings, workspace_id, source_key, cost_micros, category="agent_run"):
    async def insert():
        async with await psycopg.AsyncConnection.connect(
            settings.database_url.get_secret_value()
        ) as connection:
            return await record_spend(
                connection,
                workspace_id=UUID(workspace_id),
                source_key=source_key,
                category=category,
                provider="test",
                model="test-model",
                input_tokens=100,
                output_tokens=50,
                cost_micros=cost_micros,
            )

    return asyncio.run(insert())


def test_spend_api_reports_period_usage_and_guards_budget_changes(keys, auth_settings):
    migrate(auth_settings)
    prefix = "spend-" + str(uuid4())
    owner, admin, member, outsider = [
        prefix + suffix for suffix in ("owner", "admin", "member", "outsider")
    ]

    def headers(subject):
        return {
            "Authorization": "Bearer " + token(keys, subject),
            "x-request-id": "spend-integration",
        }

    with TestClient(create_app(settings=auth_settings)) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"name": "Spend workspace"}, headers=headers(owner)
        ).json()["id"]
        other_id = client.post(
            "/api/v1/workspaces", json={"name": "Other workspace"}, headers=headers(outsider)
        ).json()["id"]
        base = f"/api/v1/workspaces/{workspace_id}"
        for subject, role in ((admin, "admin"), (member, "member")):
            granted = client.put(
                base + "/members", json={"subject": subject, "role": role}, headers=headers(owner)
            )
            assert granted.status_code == 200, granted.text

        empty = client.get(base + "/spend", headers=headers(member))
        assert empty.status_code == 200, empty.text
        assert empty.json()["consumed_micros"] == 0
        assert empty.json()["monthly_limit_micros"] is None
        assert empty.json()["remaining_micros"] is None
        assert empty.json()["exhausted"] is False
        assert empty.json()["categories"] == []
        assert empty.json()["period_start"] < empty.json()["period_end"]

        # Reading spend is a membership right; changing a cap is not.
        assert client.get(base + "/spend", headers=headers(outsider)).status_code == 404
        denied = client.put(
            base + "/spend/budget",
            json={"monthly_limit_micros": 5_000, "enforcement": "enforce"},
            headers=headers(member),
        )
        assert denied.status_code == 403
        assert (
            client.put(
                base + "/spend/budget",
                json={"monthly_limit_micros": 5_000},
                headers=headers(outsider),
            ).status_code
            == 404
        )
        invalid = client.put(
            base + "/spend/budget",
            json={"monthly_limit_micros": -1},
            headers=headers(admin),
        )
        assert invalid.status_code == 422

        created = client.put(
            base + "/spend/budget",
            json={"monthly_limit_micros": 5_000, "enforcement": "enforce"},
            headers=headers(admin),
        )
        assert created.status_code == 200, created.text
        assert created.json()["monthly_limit_micros"] == 5_000
        assert created.json()["enforcement"] == "enforce"

        raised = client.put(
            base + "/spend/budget",
            json={"monthly_limit_micros": 9_000, "enforcement": "monitor"},
            headers=headers(owner),
        )
        assert raised.status_code == 200
        assert raised.json()["monthly_limit_micros"] == 9_000

        assert seed_record(auth_settings, workspace_id, "manual:one", 4_000) is True
        assert seed_record(auth_settings, workspace_id, "manual:one", 4_000) is False
        assert (
            seed_record(
                auth_settings, workspace_id, "manual:two", 6_000, category="evaluation_judge"
            )
            is True
        )
        assert seed_record(auth_settings, other_id, "manual:three", 99_000) is True

        summary = client.get(base + "/spend", headers=headers(member)).json()
        assert summary["consumed_micros"] == 10_000
        assert summary["monthly_limit_micros"] == 9_000
        assert summary["remaining_micros"] == 0
        # Monitor mode reports the overage without blocking execution.
        assert summary["exhausted"] is False
        assert [category["category"] for category in summary["categories"]] == [
            "agent_run",
            "evaluation_judge",
        ]
        assert summary["categories"][0]["cost_micros"] == 4_000
        assert summary["categories"][0]["call_count"] == 1
        assert summary["categories"][1]["input_tokens"] == 100

        enforced = client.put(
            base + "/spend/budget",
            json={"monthly_limit_micros": 9_000, "enforcement": "enforce"},
            headers=headers(owner),
        )
        assert enforced.status_code == 200
        assert client.get(base + "/spend", headers=headers(member)).json()["exhausted"] is True

        records = client.get(base + "/spend/records?limit=1", headers=headers(member))
        assert records.status_code == 200, records.text
        assert len(records.json()["items"]) == 1
        cursor = records.json()["next_cursor"]
        assert cursor is not None
        page_two = client.get(
            base + f"/spend/records?limit=1&cursor={cursor}", headers=headers(member)
        ).json()
        assert len(page_two["items"]) == 1
        assert page_two["items"][0]["id"] != records.json()["items"][0]["id"]
        assert page_two["next_cursor"] is None

        filtered = client.get(
            base + "/spend/records?category=evaluation_judge", headers=headers(member)
        ).json()
        assert [item["source_key"] for item in filtered["items"]] == ["manual:two"]

        # A record of another tenant is neither listed nor addressable as a cursor.
        keys_seen = {item["source_key"] for item in records.json()["items"]} | {
            item["source_key"] for item in page_two["items"]
        }
        assert "manual:three" not in keys_seen
        foreign_id = client.get(
            f"/api/v1/workspaces/{other_id}/spend/records", headers=headers(outsider)
        ).json()["items"][0]["id"]
        assert (
            client.get(
                base + f"/spend/records?cursor={foreign_id}", headers=headers(member)
            ).status_code
            == 404
        )

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        events = connection.execute(
            """SELECT monthly_limit_micros,enforcement FROM workspace_spend_budget_events
               WHERE workspace_id=%s ORDER BY created_at,id""",
            (UUID(workspace_id),),
        ).fetchall()
        assert events == [(5_000, "enforce"), (9_000, "monitor"), (9_000, "enforce")]
        audited = connection.execute(
            """SELECT count(*) FROM security_events
               WHERE workspace_id=%s AND action='spend.budget.set'""",
            (UUID(workspace_id),),
        ).fetchone()[0]
        assert audited == 3
        for statement in (
            "UPDATE workspace_spend_records SET cost_micros=0 WHERE workspace_id=%s",
            "DELETE FROM workspace_spend_records WHERE workspace_id=%s",
            "UPDATE workspace_spend_budget_events SET monthly_limit_micros=0 WHERE workspace_id=%s",
        ):
            with psycopg.connect(auth_settings.database_url.get_secret_value()) as ledger:
                with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                    ledger.execute(statement, (UUID(workspace_id),))


def build_executor(auth_settings, workspace_id, provider, spend):
    return DurableAgentExecutor(
        store=ExecutorStore(auth_settings, spend=spend),
        router=ModelRouter([ModelCandidate("test", "test-model", frozenset(ModelCapability))]),
        adapters={"test": provider},
        gateway=McpGateway(auth_settings),
        profiles={
            "default": ExecutionProfile(frozenset({UUID(workspace_id)}), frozenset({"test"}))
        },
    )


def run_worker(auth_settings, executor):
    async def execute():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            return await AgentWorker(auth_settings, redis, executor).process_once()
        finally:
            await redis.aclose()

    return asyncio.run(execute())


def test_worker_prices_each_model_step_into_the_durable_ledger(keys, auth_settings):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    client, headers, workspace_id, run_id = make_runtime(keys, auth_settings, "spend-ledger")
    provider = Adapter([response("Priced answer")])
    cost_before = (
        metrics.REGISTRY.get_sample_value(
            "nexora_model_cost_micros_total", {"provider": "test", "category": "agent_run"}
        )
        or 0.0
    )
    try:
        executor = build_executor(auth_settings, workspace_id, provider, policy())
        assert run_worker(auth_settings, executor)
        assert len(provider.requests) == 1
        result = client.get(f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers())
        assert result.json()["status"] == "succeeded", result.text

        spend = client.get(f"/api/v1/workspaces/{workspace_id}/spend", headers=headers()).json()
        # The stub reports 10 input and 1 output token: 10 * 1 + 1 * 2 micros.
        assert spend["consumed_micros"] == 12
        assert spend["categories"] == [
            {
                "category": "agent_run",
                "call_count": 1,
                "input_tokens": 10,
                "output_tokens": 1,
                "cost_micros": 12,
            }
        ]
        records = client.get(
            f"/api/v1/workspaces/{workspace_id}/spend/records", headers=headers()
        ).json()["items"]
        assert [record["source_key"] for record in records] == [
            agent_step_source_key(UUID(run_id), 0)
        ]
        assert records[0]["model"] == "test-model"
        assert (
            metrics.REGISTRY.get_sample_value(
                "nexora_model_cost_micros_total", {"provider": "test", "category": "agent_run"}
            )
            == cost_before + 12
        )

        events = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/events", headers=headers()
        ).json()["items"]
        completed = [event for event in events if event["event_type"] == "model.completed"]
        assert completed[0]["payload"]["cost_micros"] == 12

        # A replayed step must not be charged again.
        assert (
            seed_record(auth_settings, workspace_id, agent_step_source_key(UUID(run_id), 0), 12)
            is False
        )
        assert (
            client.get(f"/api/v1/workspaces/{workspace_id}/spend", headers=headers()).json()[
                "consumed_micros"
            ]
            == 12
        )
    finally:
        client.__exit__(None, None, None)


def test_exhausted_budget_stops_the_run_before_provider_egress(keys, auth_settings):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    client, headers, workspace_id, run_id = make_runtime(keys, auth_settings, "spend-budget")
    denied_before = (
        metrics.REGISTRY.get_sample_value("nexora_spend_denials_total", {"category": "agent_run"})
        or 0.0
    )
    try:
        budget = client.put(
            f"/api/v1/workspaces/{workspace_id}/spend/budget",
            json={"monthly_limit_micros": 10, "enforcement": "enforce"},
            headers=headers(),
        )
        assert budget.status_code == 200, budget.text
        assert seed_record(auth_settings, workspace_id, "prior-period-usage", 10) is True

        provider = Adapter([response("Must never be requested")])
        executor = build_executor(auth_settings, workspace_id, provider, policy())
        assert run_worker(auth_settings, executor)
        assert provider.requests == []

        run = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers()
        ).json()
        assert run["status"] == "failed"
        assert run["failure_code"] == "workspace_budget_exhausted"
        events = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/events", headers=headers()
        ).json()["items"]
        denied = [event for event in events if event["event_type"] == "spend.denied"]
        assert denied and denied[0]["payload"] == {"limit_micros": 10, "consumed_micros": 10}
        assert (
            metrics.REGISTRY.get_sample_value(
                "nexora_spend_denials_total", {"category": "agent_run"}
            )
            == denied_before + 1
        )
        # Nothing was called, so nothing was charged beyond the seeded usage.
        assert (
            client.get(f"/api/v1/workspaces/{workspace_id}/spend", headers=headers()).json()[
                "consumed_micros"
            ]
            == 10
        )
    finally:
        client.__exit__(None, None, None)


def test_unpriced_model_fails_closed_instead_of_recording_free_usage(keys, auth_settings):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    client, headers, workspace_id, run_id = make_runtime(keys, auth_settings, "spend-unpriced")
    try:
        provider = Adapter([response("Unpriced answer")])
        executor = build_executor(
            auth_settings, workspace_id, provider, policy(model="other-model")
        )
        assert run_worker(auth_settings, executor)
        run = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers()
        ).json()
        assert run["status"] == "failed"
        assert run["failure_code"] == "model_price_not_configured"
        assert (
            client.get(
                f"/api/v1/workspaces/{workspace_id}/spend/records", headers=headers()
            ).json()["items"]
            == []
        )
    finally:
        client.__exit__(None, None, None)


def test_judge_worker_refuses_paid_scoring_when_the_budget_is_exhausted(keys, auth_settings):
    migrate(auth_settings)
    subject = "spend-judge-" + str(uuid4())

    def headers():
        return {"Authorization": "Bearer " + token(keys, subject)}

    with TestClient(create_app(settings=auth_settings)) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"name": "Judge budget workspace"}, headers=headers()
        ).json()["id"]
        assert (
            client.put(
                f"/api/v1/workspaces/{workspace_id}/spend/budget",
                json={"monthly_limit_micros": 100, "enforcement": "enforce"},
                headers=headers(),
            ).status_code
            == 200
        )
        assert seed_record(auth_settings, workspace_id, "judge-prior-usage", 100) is True

    adapter = NamedAdapter([])
    worker = EvaluationJudgeWorker(
        auth_settings,
        adapter,
        EvaluationJudgeConfig(
            provider="test",
            model="judge-model-v1",
            allowed_workspaces=[UUID(workspace_id)],
        ),
        worker_id="spend-judge-worker",
        spend=SpendPolicy(prices={("test", "judge-model-v1"): PRICE}),
    )
    denied_before = (
        metrics.REGISTRY.get_sample_value(
            "nexora_spend_denials_total", {"category": "evaluation_judge"}
        )
        or 0.0
    )
    with pytest.raises(JudgeExecutionError, match="judge_budget_exhausted"):
        asyncio.run(worker._assert_budget({"workspace_id": UUID(workspace_id)}))
    assert adapter.requests == []
    assert (
        metrics.REGISTRY.get_sample_value(
            "nexora_spend_denials_total", {"category": "evaluation_judge"}
        )
        == denied_before + 1
    )


EMBEDDING_PRICE = ModelPrice(
    input_micros_per_million_tokens=1_000_000,
    output_micros_per_million_tokens=0,
)


def embedding_spend():
    return EmbeddingSpend(provider="stub", model="embed-test", price=EMBEDDING_PRICE)


class TracingEmbeddingAdapter(StubEmbeddingAdapter):
    """Records which job each embedding call belongs to.

    An ingestion worker claims queued work from any workspace, so a call count alone
    cannot prove that this test's job did or did not reach the provider.
    """

    def __init__(self):
        super().__init__()
        self.request_ids = []

    async def embed(self, *, request_id, **kwargs):
        self.request_ids.append(request_id)
        return await super().embed(request_id=request_id, **kwargs)

    def called_for(self, job_id) -> bool:
        return any(str(job_id) in request_id for request_id in self.request_ids)


def run_until_terminal(worker, client, path, headers):
    """Process jobs until this test's job finishes; other suites leave queued work behind."""

    async def drain():
        for _ in range(25):
            if not await worker.process_once():
                return
            if client.get(path, headers=headers).json()["status"] in ("succeeded", "failed"):
                return

    asyncio.run(drain())
    return client.get(path, headers=headers).json()


def priced_pipeline(auth_settings, adapter, spend=None):
    return RagEmbeddingPipeline(
        RagRepository(auth_settings),
        adapter,
        embedding_model="embed-test",
        dimensions=3,
        batch_size=8,
        timeout_seconds=3,
        spend=embedding_spend() if spend is None else spend,
    )


def test_ingestion_embeddings_are_priced_with_the_indexed_version(keys, auth_settings):
    migrate(auth_settings)
    client, headers, workspace_id, owner, admin, member, _ = setup_workspace(
        keys, auth_settings, "spend-ingestion"
    )
    try:
        base = f"/api/v1/workspaces/{workspace_id}/knowledge"
        body = {
            "source_key": "handbook",
            "version": "v1",
            "title": "Engineering Handbook",
            "text": "Approved engineering guidance. " * 60,
            "max_chars": 300,
            "overlap_chars": 20,
        }
        queued = client.post(base + "/sources", json=body, headers=headers(admin, "spend-ingest-1"))
        assert queued.status_code == 202, queued.text

        adapter = TracingEmbeddingAdapter()
        worker = KnowledgeIngestionWorker(
            auth_settings, priced_pipeline(auth_settings, adapter), worker_id="spend-ingest-worker"
        )
        job_path = base + f"/ingestions/{queued.json()['id']}"
        job = run_until_terminal(worker, client, job_path, headers(admin))
        assert job["status"] == "succeeded", job
        assert adapter.called_for(job["id"])
        tokens = job["embedding_input_tokens"]
        assert tokens > 0

        summary = client.get(f"/api/v1/workspaces/{workspace_id}/spend", headers=headers(owner))
        spend = summary.json()
        assert spend["categories"] == [
            {
                "category": "embedding",
                "call_count": 1,
                "input_tokens": tokens,
                "output_tokens": 0,
                "cost_micros": tokens,
            }
        ]
        records = client.get(
            f"/api/v1/workspaces/{workspace_id}/spend/records", headers=headers(member)
        ).json()["items"]
        assert records[0]["source_key"] == knowledge_source_key(UUID(job["source_id"]))
        assert records[0]["model"] == "embed-test"
        assert metrics.REGISTRY.get_sample_value(
            "nexora_model_cost_micros_total", {"provider": "stub", "category": "embedding"}
        )
    finally:
        client.__exit__(None, None, None)


def test_exhausted_budget_stops_ingestion_before_any_embedding_call(keys, auth_settings):
    migrate(auth_settings)
    client, headers, workspace_id, owner, admin, _, _ = setup_workspace(
        keys, auth_settings, "spend-ingestion-budget"
    )
    try:
        assert (
            client.put(
                f"/api/v1/workspaces/{workspace_id}/spend/budget",
                json={"monthly_limit_micros": 5, "enforcement": "enforce"},
                headers=headers(owner),
            ).status_code
            == 200
        )
        assert seed_record(auth_settings, workspace_id, "prior-embedding-usage", 5) is True

        base = f"/api/v1/workspaces/{workspace_id}/knowledge"
        queued = client.post(
            base + "/sources",
            json={
                "source_key": "handbook",
                "version": "v1",
                "title": "Engineering Handbook",
                "text": "Approved engineering guidance. " * 60,
            },
            headers=headers(admin, "spend-ingest-budget-1"),
        )
        assert queued.status_code == 202, queued.text

        adapter = TracingEmbeddingAdapter()
        worker = KnowledgeIngestionWorker(
            auth_settings, priced_pipeline(auth_settings, adapter), worker_id="spend-budget-worker"
        )
        denied_before = (
            metrics.REGISTRY.get_sample_value(
                "nexora_spend_denials_total", {"category": "embedding"}
            )
            or 0.0
        )
        job_path = base + f"/ingestions/{queued.json()['id']}"
        job = run_until_terminal(worker, client, job_path, headers(admin))
        # An exhausted budget is terminal: retrying would only repeat the refusal.
        assert job["status"] == "failed"
        assert job["error_code"] == "workspace_budget_exhausted"
        assert not adapter.called_for(job["id"])
        assert (
            metrics.REGISTRY.get_sample_value(
                "nexora_spend_denials_total", {"category": "embedding"}
            )
            == denied_before + 1
        )
        assert (
            client.get(
                f"/api/v1/workspaces/{workspace_id}/spend/records", headers=headers(owner)
            ).json()["items"][0]["source_key"]
            == "prior-embedding-usage"
        )
    finally:
        client.__exit__(None, None, None)


def test_query_embeddings_are_priced_and_never_run_unattributed(keys, auth_settings):
    migrate(auth_settings)
    client, headers, workspace_id, owner, admin, _, _ = setup_workspace(
        keys, auth_settings, "spend-retrieval"
    )
    try:
        adapter = StubEmbeddingAdapter()
        pipeline = priced_pipeline(auth_settings, adapter)
        principal = Principal(auth_settings.auth_issuer, admin)
        run_id = uuid4()

        async def retrieve(**kwargs):
            return await pipeline.retrieve(
                principal,
                UUID(workspace_id),
                query="What is approved?",
                request_id="spend-retrieval-test",
                **kwargs,
            )

        # A query embedding with nowhere to charge it is refused before egress.
        with pytest.raises(SpendPricingError, match="embedding_spend_key_missing"):
            asyncio.run(retrieve())
        assert adapter.calls == 0

        asyncio.run(retrieve(spend_key=run_retrieval_source_key(run_id)))
        assert adapter.calls == 1
        records = client.get(
            f"/api/v1/workspaces/{workspace_id}/spend/records?category=embedding",
            headers=headers(owner),
        ).json()["items"]
        assert [record["source_key"] for record in records] == [run_retrieval_source_key(run_id)]
        assert records[0]["output_tokens"] == 0
        charged = records[0]["cost_micros"]
        assert charged > 0

        # A resumed run that retries the same retrieval is not charged twice.
        asyncio.run(retrieve(spend_key=run_retrieval_source_key(run_id)))
        assert adapter.calls == 2
        assert (
            client.get(f"/api/v1/workspaces/{workspace_id}/spend", headers=headers(owner)).json()[
                "consumed_micros"
            ]
            == charged
        )
    finally:
        client.__exit__(None, None, None)
