import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nexora_api.executor import DurableAgentExecutor, ExecutionProfile
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
from nexora_api.run_state import ExecutionContext
from nexora_api.worker import RetryableExecutionError, TerminalExecutionError


class Store:
    def __init__(self):
        self.steps = {}
        self.checks = 0

    async def check(self, context):
        self.checks += 1
        return {"requested_by_issuer": "issuer", "requested_by_subject": "subject"}

    async def load(self, context, step):
        return self.steps.get(step)

    async def save(self, context, step, decision, response):
        assert step not in self.steps
        self.steps[step] = response


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


class Gateway:
    def __init__(self):
        self.repository = self
        self.pending = False
        self.keys = []
        self.executions = 0
        self.results = {}

    async def list_tools(self, *args):
        return [SimpleNamespace(name="lookup", description="Lookup", input_schema={}, enabled=True)]

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
