import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nexora_api.config import Settings
from nexora_api.mcp_gateway import McpAdapterError, McpGateway, McpGatewayError
from nexora_api.run_state import ExecutionContext


class FailingAdapter:
    def __init__(self, error):
        self.error = error

    async def call_tool(self, remote_name, arguments, timeout_seconds):
        raise self.error


class Repository:
    def __init__(self, server_key="remote", remote_name="remote_search"):
        self.call_id = uuid4()
        self.failures = []
        self.server_key = server_key
        self.remote_name = remote_name
        self.prepared_tools = []

    async def prepare_call(self, context, call_key, tool_name, arguments):
        self.prepared_tools.append(tool_name)
        return SimpleNamespace(
            action="execute",
            call_id=self.call_id,
            tool=SimpleNamespace(server_key=self.server_key, side_effect="read"),
        )

    async def mark_running(self, call_id, context):
        return SimpleNamespace(
            call_id=call_id,
            server_key=self.server_key,
            remote_name=self.remote_name,
            arguments={"q": "otters"},
            output_schema=None,
        )

    async def complete_failure(self, call_id, error_code, *, retryable, context):
        self.failures.append((call_id, error_code, retryable))
        return True

    async def complete_success(self, call_id, result, context):
        return True


def context(**overrides):
    fields = {
        "job_id": uuid4(),
        "workspace_id": uuid4(),
        "run_id": uuid4(),
        "agent_id": uuid4(),
        "trace_id": uuid4(),
        "input_text": "search",
        "instructions": "use governed tools",
        "model_profile": "default",
        "attempt_count": 1,
    }
    fields.update(overrides)
    return ExecutionContext(**fields)


@pytest.mark.parametrize("retryable", [False, True])
def test_adapter_failure_preserves_retry_semantics(retryable):
    error = McpAdapterError("mcp_test_error", retryable=retryable)
    gateway = McpGateway(Settings(), adapters={"remote": FailingAdapter(error)})
    repository = Repository()
    gateway.repository = repository

    with pytest.raises(McpGatewayError) as raised:
        asyncio.run(
            gateway.invoke(
                context(),
                "tool-step-1",
                "search",
                {"q": "otters"},
            )
        )

    assert raised.value.code == "mcp_test_error"
    assert raised.value.retryable is retryable
    assert repository.failures == [(repository.call_id, "mcp_test_error", retryable)]



class ContextAdapter:
    def __init__(self):
        self.calls = []

    async def call_tool(self, execution_context, remote_name, arguments, timeout_seconds):
        self.calls.append((execution_context, remote_name, arguments, timeout_seconds))
        return {"ok": True}


def test_standard_agent_alias_is_governed_then_executed_with_run_context():
    execution_context = context(
        agent_kind="standard",
        tool_aliases={"mikro.stock.read": "connector.0123456789abcdef"},
    )
    adapter = ContextAdapter()
    gateway = McpGateway(Settings(), context_adapters={"connector": adapter})
    repository = Repository(server_key="connector", remote_name="mikro.stock.read")
    gateway.repository = repository

    result = asyncio.run(
        gateway.invoke(
            execution_context,
            "tool-step-1",
            "mikro.stock.read",
            {},
        )
    )

    assert result == {"ok": True}
    assert repository.prepared_tools == ["connector.0123456789abcdef"]
    assert len(adapter.calls) == 1
    called_context, remote_name, arguments, timeout = adapter.calls[0]
    assert called_context is execution_context
    assert remote_name == "mikro.stock.read"
    assert arguments == {"q": "otters"}
    assert timeout == gateway.timeout_seconds
