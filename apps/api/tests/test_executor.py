import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nexora_api import metrics
from nexora_api.executor import DurableAgentExecutor, ExecutionProfile
from nexora_api.executor_store import RunRetrievalSnapshot
from nexora_api.mcp_gateway import ApprovalRequired
from nexora_api.model_routing import (
    ModelCandidate,
    ModelCapability,
    ModelRouter,
    ProviderError,
    ProviderResponse,
    ProviderToolCall,
    ProviderUsage,
)
from nexora_api.rag import RetrievedChunk, build_untrusted_context
from nexora_api.rag_pipeline import RagSearchResult
from nexora_api.run_state import ExecutionContext
from nexora_api.spend import SpendLimitExceeded, run_retrieval_source_key
from nexora_api.tool_contracts import ToolContractError
from nexora_api.worker import RetryableExecutionError, TerminalExecutionError


class Store:
    def __init__(self, budget_allows=True):
        self.steps = {}
        self.checks = 0
        self.retrieval = None
        self.budget_allows = budget_allows
        self.budget_checks = 0
        self.answer_cache = {}
        self.cache_lookups = 0

    async def authorize_spend(self, context):
        self.budget_checks += 1
        if not self.budget_allows:
            raise ToolContractError("workspace_budget_exhausted")

    async def check(self, context):
        self.checks += 1
        return {"requested_by_issuer": "issuer", "requested_by_subject": "subject"}

    async def load_retrieval(self, context):
        return self.retrieval

    async def save_retrieval(self, context, result):
        text = build_untrusted_context(result.chunks) if result.chunks else ""
        self.retrieval = RunRetrievalSnapshot(
            text, result.embedding_input_tokens, len(result.chunks)
        )
        return self.retrieval

    async def load(self, context, step):
        return self.steps.get(step)

    async def restore_answer_cache(self, context, step, decision, cache_key):
        self.cache_lookups += 1
        cached = self.answer_cache.get((context.workspace_id, context.agent_id, cache_key))
        if cached is None:
            return None
        assert step not in self.steps
        self.steps[step] = cached
        return cached

    async def save(
        self,
        context,
        step,
        decision,
        response,
        *,
        cache_key=None,
        cache_ttl_seconds=0,
    ):
        assert step not in self.steps
        self.steps[step] = response
        if cache_key is not None and cache_ttl_seconds > 0:
            self.answer_cache[(context.workspace_id, context.agent_id, cache_key)] = replace(
                response, usage=ProviderUsage(0, 0)
            )


class Adapter:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def generate(self, **kwargs):
        self.requests.append(kwargs["request"])
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class Retriever:
    def __init__(self, result, error=None):
        self.result = result
        self.error = error
        self.calls = 0
        self.spend_keys = []

    async def retrieve(self, *args, **kwargs):
        self.calls += 1
        self.spend_keys.append(kwargs.get("spend_key"))
        if self.error is not None:
            raise self.error
        return self.result


class Gateway:
    def __init__(self):
        self.repository = self
        self.pending = False
        self.keys = []
        self.executions = 0
        self.results = {}
        self.tools = [
            SimpleNamespace(name="lookup", description="Lookup", input_schema={}, enabled=True)
        ]

    async def list_tools(self, *args):
        return self.tools

    async def invoke(self, context, key, name, args, cancelled):
        self.keys.append(key)
        if self.pending:
            raise ApprovalRequired(uuid4(), uuid4())
        if key not in self.results:
            self.executions += 1
            self.results[key] = {"value": 42}
        return self.results[key]


def response(text="Done", calls=(), reason="stop", tokens=10):
    return ProviderResponse(text, calls, None, ProviderUsage(tokens, 1), reason)


def setup(responses, **limits):
    context = ExecutionContext(
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), "Input", "System", "default", 1
    )
    store, gateway, adapter = Store(), Gateway(), Adapter(responses)
    profile = ExecutionProfile(
        frozenset({context.workspace_id}), frozenset({"test"}), frozenset({"lookup"}), **limits
    )
    executor = DurableAgentExecutor(
        store=store,
        router=ModelRouter([ModelCandidate("test", "test-model", frozenset(ModelCapability))]),
        adapters={"test": adapter},
        gateway=gateway,
        profiles={"default": profile},
    )
    return executor, context, store, gateway, adapter


async def not_cancelled():
    return False


def test_text_response_is_persisted_and_replayed():
    executor, context, store, _, adapter = setup([response()])
    asyncio.run(executor.execute(context, not_cancelled))
    asyncio.run(executor.execute(context, not_cancelled))
    assert len(adapter.requests) == 1
    assert store.steps[0].text == "Done"


def test_workspace_answer_cache_reuses_normalized_question_without_provider_cost():
    executor, context, store, gateway, adapter = setup(
        [response("Cached answer"), response("Other tenant answer")],
        answer_cache_ttl_seconds=3600,
    )
    gateway.tools = []
    misses_before = sample("nexora_answer_cache_events_total", outcome="miss")
    hits_before = sample("nexora_answer_cache_events_total", outcome="hit")
    stored_before = sample("nexora_answer_cache_events_total", outcome="stored")

    asyncio.run(executor.execute(context, not_cancelled))
    assert len(adapter.requests) == 1
    assert store.budget_checks == 1
    assert len(store.answer_cache) == 1

    store.steps.clear()
    repeated = replace(context, run_id=uuid4(), input_text="  INPUT   ")
    asyncio.run(executor.execute(repeated, not_cancelled))
    assert len(adapter.requests) == 1
    assert store.budget_checks == 1
    assert store.steps[0].text == "Cached answer"
    assert store.steps[0].usage == ProviderUsage(0, 0)

    other_workspace = uuid4()
    profile = executor.profiles["default"]
    executor.profiles["default"] = replace(
        profile,
        allowed_workspaces=frozenset({context.workspace_id, other_workspace}),
    )
    store.steps.clear()
    other_tenant = replace(context, workspace_id=other_workspace, run_id=uuid4())
    asyncio.run(executor.execute(other_tenant, not_cancelled))
    assert len(adapter.requests) == 2
    assert adapter.requests[-1].messages[-1].content == "Input"
    assert store.steps[0].text == "Other tenant answer"

    assert sample("nexora_answer_cache_events_total", outcome="miss") == misses_before + 2
    assert sample("nexora_answer_cache_events_total", outcome="hit") == hits_before + 1
    assert sample("nexora_answer_cache_events_total", outcome="stored") == stored_before + 2


def test_answer_cache_is_not_used_when_tools_or_retrieval_can_change_the_answer():
    executor, context, store, gateway, adapter = setup(
        [response("First"), response("Second")],
        answer_cache_ttl_seconds=3600,
    )
    asyncio.run(executor.execute(context, not_cancelled))
    assert store.answer_cache == {}

    store.steps.clear()
    asyncio.run(executor.execute(replace(context, run_id=uuid4()), not_cancelled))
    assert len(adapter.requests) == 2
    assert store.cache_lookups == 0

    executor, context, store, gateway, adapter = setup(
        [response("Retrieved")],
        answer_cache_ttl_seconds=3600,
    )
    gateway.tools = []
    executor.retriever = Retriever(RagSearchResult((), 3))
    asyncio.run(executor.execute(context, not_cancelled))
    assert store.answer_cache == {}
    assert store.cache_lookups == 0


def test_retrieval_snapshot_is_reused_without_second_embedding_or_search():
    executor, context, store, _, adapter = setup([response()])
    evidence = RetrievedChunk(
        id=uuid4(),
        source_id=uuid4(),
        source_key="handbook",
        source_version="v1",
        title="Handbook",
        chunk_index=0,
        start_offset=0,
        end_offset=17,
        content="approved evidence",
        metadata={},
        score=0.9,
    )
    retriever = Retriever(RagSearchResult((evidence,), 3))
    executor.retriever = retriever

    asyncio.run(executor.execute(context, not_cancelled))
    asyncio.run(executor.execute(replace(context, attempt_count=2), not_cancelled))

    assert retriever.calls == 1
    assert store.retrieval.chunk_count == 1
    assert len(adapter.requests) == 1
    assert "source=handbook version=v1 chunk=0" in adapter.requests[0].messages[2].content


def test_retrieval_is_charged_to_its_run_and_a_spent_budget_fails_it_closed():
    executor, context, store, _, adapter = setup([response()])
    executor.retriever = Retriever(RagSearchResult((), 3))
    asyncio.run(executor.execute(context, not_cancelled))
    assert executor.retriever.spend_keys == [run_retrieval_source_key(context.run_id)]

    executor, context, store, _, adapter = setup([response()])
    executor.retriever = Retriever(None, error=SpendLimitExceeded())
    with pytest.raises(TerminalExecutionError, match="workspace_budget_exhausted"):
        asyncio.run(executor.execute(context, not_cancelled))
    # No model call may follow a refused retrieval.
    assert adapter.requests == []


def test_approval_resume_uses_persisted_decision_and_call_identity():
    call = ProviderToolCall("call-1", "lookup", {"query": "hello"})
    executor, context, store, gateway, adapter = setup(
        [response(None, (call,), "tool_call"), response()]
    )
    gateway.pending = True
    with pytest.raises(ApprovalRequired):
        asyncio.run(executor.execute(context, not_cancelled))
    assert 0 in store.steps and gateway.executions == 0
    gateway.pending = False
    asyncio.run(executor.execute(replace(context, attempt_count=2), not_cancelled))
    assert len(adapter.requests) == 2
    assert gateway.keys == ["step:0:tool:0", "step:0:tool:0"]
    assert gateway.executions == 1
    messages = adapter.requests[-1].messages
    assert messages[-2].tool_call == call
    assert messages[-1].tool_call_id == "call-1"


@pytest.mark.parametrize(
    "result,code",
    [
        (response(reason="incomplete"), "model_response_incomplete"),
        (response(text=None), "empty_model_response"),
        (response(tokens=-1), "invalid_model_response"),
        (response(tokens=100), "token_budget_exceeded"),
        (
            response(None, (ProviderToolCall("1", "unknown", {}),), "tool_call"),
            "model_tool_not_allowed",
        ),
    ],
)
def test_invalid_or_unbounded_responses_fail(result, code):
    executor, context, _, gateway, _ = setup([result], max_total_tokens=50)
    with pytest.raises(TerminalExecutionError, match=code):
        asyncio.run(executor.execute(context, not_cancelled))
    assert gateway.executions == 0


def test_workspace_profile_is_fail_closed():
    executor, context, _, _, adapter = setup([response()])
    with pytest.raises(TerminalExecutionError, match="model_profile_not_authorized"):
        asyncio.run(executor.execute(replace(context, workspace_id=uuid4()), not_cancelled))
    assert not adapter.requests


def test_retryable_provider_failure_does_not_commit_response():
    executor, context, store, _, _ = setup([ProviderError("provider_timeout", retryable=True)])
    with pytest.raises(RetryableExecutionError, match="provider_timeout"):
        asyncio.run(executor.execute(context, not_cancelled))
    assert not store.steps


def test_cancellation_interrupts_provider_and_persists_nothing():
    executor, context, store, _, adapter = setup([])
    cancelled_task = []

    async def generate(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled_task.append(True)

    adapter.generate = generate
    checks = 0

    async def cancellation():
        nonlocal checks
        checks += 1
        return checks > 1

    with pytest.raises(TerminalExecutionError, match="cancel_requested"):
        asyncio.run(executor.execute(context, cancellation))
    assert cancelled_task and not store.steps


def sample(name, **labels):
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def test_model_usage_and_failures_are_measured():
    executor, context, _, _, _ = setup([response(tokens=7)])
    tokens_before = sample("nexora_model_tokens_total", provider="test", kind="input")
    calls_before = sample("nexora_model_calls_total", provider="test", outcome="success")

    asyncio.run(executor.execute(context, not_cancelled))

    assert sample("nexora_model_tokens_total", provider="test", kind="input") == tokens_before + 7
    assert sample("nexora_model_calls_total", provider="test", outcome="success") == (
        calls_before + 1
    )

    failing, failing_context, _, _, _ = setup([ProviderError("provider_error", retryable=True)])
    errors_before = sample("nexora_model_calls_total", provider="test", outcome="error")

    with pytest.raises(RetryableExecutionError):
        asyncio.run(failing.execute(failing_context, not_cancelled))

    assert sample("nexora_model_calls_total", provider="test", outcome="error") == (
        errors_before + 1
    )
