"""Run-scoped internal task tools. They never perform an external customer action."""
from typing import Any

from nexora_api.config import Settings
from nexora_api.mcp_gateway import McpAdapterError
from nexora_api.task_repository import RunTaskError, RunTaskRepository

TASK_SERVER_KEY = "tasks"


class TaskMcpAdapter:
    def __init__(self, settings: Settings, repository: RunTaskRepository | None = None):
        self.repository = repository or RunTaskRepository(settings)

    async def call_tool_for_context(self, context, remote_name: str,
                                    arguments: dict[str, object],
                                    timeout_seconds: float) -> Any:
        del timeout_seconds
        if context.agent_kind != "standard":
            raise McpAdapterError("task_standard_run_required", retryable=False)
        try:
            if remote_name == "child.create":
                return await self.repository.create_follow_up(context, arguments)
            if remote_name == "task.verify":
                return await self.repository.verify(context, arguments)
        except RunTaskError as exc:
            raise McpAdapterError(exc.code, retryable=False) from exc
        raise McpAdapterError("task_operation_unknown", retryable=False)
