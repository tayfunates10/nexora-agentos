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
    def __init__(self):
        self.call_id = uuid4()
        self.failures = []

    async def prepare_call(self, context, call_key, tool_name, arguments):
        return SimpleNamespace(
            action="execute",
            call_id=self.call_id,
            tool=SimpleNamespace(server_key="remote", side_effect="read"),
        )

    async def mark_running(self, call_id, context):
        return SimpleNamespace(
            call_id=call_id,
            server_key="remote",
            remote_name="remote_search",
            arguments={"q": "otters"},
            output_schema=None,
        )

    async def complete_failure(self, call_id, error_code, *, retryable, context):
        self.failures.append((call_id, error_code, retryable))
        return True


def context():
    return ExecutionContext(
        job_id=uuid4(),
        workspace_id=uuid4(),
        run_id=uuid4(),
        agent_id=uuid4(),
        trace_id=uuid4(),
        input_text="search",
        instructions="use governed tools",
        model_profile="default",
        attempt_count=1,
    )


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
