import asyncio
from types import SimpleNamespace

import pytest

from nexora_api.mcp_gateway import McpAdapterError
from nexora_api.task_mcp import TaskMcpAdapter
from nexora_api.task_repository import RunTaskError


class Repository:
    def __init__(self):
        self.calls = []

    async def create_follow_up(self, context, arguments):
        self.calls.append(("create", context, arguments))
        return {"task_id": "00000000-0000-0000-0000-000000000001", "status": "planned"}

    async def verify(self, context, arguments):
        self.calls.append(("verify", context, arguments))
        return {"task_id": arguments["task_id"], "status": "succeeded"}


class FailingRepository(Repository):
    async def verify(self, context, arguments):
        raise RunTaskError("task_verification_evidence_missing")


def context(kind="standard"):
    return SimpleNamespace(agent_kind=kind)


def test_task_adapter_routes_run_scoped_operations():
    repository = Repository()
    adapter = TaskMcpAdapter.__new__(TaskMcpAdapter)
    adapter.repository = repository
    created = asyncio.run(
        adapter.call_tool_for_context(context(), "child.create", {"title": "Follow up"}, 1.0)
    )
    verified = asyncio.run(
        adapter.call_tool_for_context(
            context(), "task.verify", {"task_id": created["task_id"]}, 1.0
        )
    )
    assert created["status"] == "planned"
    assert verified["status"] == "succeeded"
    assert [item[0] for item in repository.calls] == ["create", "verify"]


def test_task_adapter_rejects_custom_agent_context():
    adapter = TaskMcpAdapter.__new__(TaskMcpAdapter)
    adapter.repository = Repository()
    with pytest.raises(McpAdapterError) as raised:
        asyncio.run(
            adapter.call_tool_for_context(context("custom"), "child.create", {"title": "No"}, 1.0)
        )
    assert raised.value.code == "task_standard_run_required"


def test_task_adapter_preserves_stable_repository_error_code():
    adapter = TaskMcpAdapter.__new__(TaskMcpAdapter)
    adapter.repository = FailingRepository()
    with pytest.raises(McpAdapterError) as raised:
        asyncio.run(
            adapter.call_tool_for_context(
                context(), "task.verify", {"task_id": "00000000-0000-0000-0000-000000000001"}, 1.0
            )
        )
    assert raised.value.code == "task_verification_evidence_missing"
    assert raised.value.retryable is False
