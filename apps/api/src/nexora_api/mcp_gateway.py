import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from nexora_api.config import Settings
from nexora_api.run_state import ExecutionContext
from nexora_api.tool_contracts import ToolContractError
from nexora_api.tool_repository import ToolGovernanceRepository


class McpToolAdapter(Protocol):
    async def call_tool(
        self,
        remote_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> Any: ...


@dataclass(frozen=True)
class ApprovalRequired(Exception):
    tool_call_id: UUID
    approval_id: UUID

    def __str__(self) -> str:
        return "tool_approval_required"


@dataclass(frozen=True)
class McpGatewayError(Exception):
    code: str
    retryable: bool

    def __str__(self) -> str:
        return self.code


class McpGateway:
    """Policy-enforcing boundary between agent executors and MCP adapters.

    Adapters are operator-provided by server_key. Workspace users can register typed
    tool metadata, but cannot inject transport URLs or credentials into the gateway.
    """

    def __init__(
        self,
        settings: Settings,
        adapters: Mapping[str, McpToolAdapter] | None = None,
        timeout_seconds: float = 15.0,
    ):
        if not 0.1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0.1 and 120")
        self.repository = ToolGovernanceRepository(settings)
        self.adapters = dict(adapters or {})
        self.timeout_seconds = timeout_seconds

    async def invoke(
        self,
        context: ExecutionContext,
        call_key: str,
        tool_name: str,
        arguments: dict[str, Any],
        is_cancelled: Callable[[], Awaitable[bool]] | None = None,
    ) -> Any:
        plan = await self.repository.prepare_call(context, call_key, tool_name, arguments)
        if plan.action == "replay":
            return plan.result
        if plan.action == "deny":
            raise McpGatewayError(plan.error_code or "tool_denied", retryable=False)
        if plan.action == "approval":
            if plan.approval_id is None:
                raise RuntimeError("approval plan missing approval_id")
            raise ApprovalRequired(plan.call_id, plan.approval_id)

        if is_cancelled is not None and await is_cancelled():
            raise McpGatewayError("cancel_requested", retryable=False)

        try:
            execution = await self.repository.mark_running(context, plan.call_id)
        except ToolContractError as exc:
            raise McpGatewayError(
                exc.code,
                retryable=exc.code == "run_lease_lost",
            ) from exc

        adapter = self.adapters.get(execution.server_key)
        if adapter is None:
            await self.repository.complete_failure(
                context,
                execution.call_id,
                "mcp_server_unavailable",
                retryable=True,
            )
            raise McpGatewayError("mcp_server_unavailable", retryable=True)

        try:
            result = await asyncio.wait_for(
                adapter.call_tool(
                    execution.remote_name,
                    execution.arguments,
                    self.timeout_seconds,
                ),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            await self.repository.complete_failure(
                context,
                execution.call_id,
                "mcp_timeout",
                retryable=True,
            )
            raise McpGatewayError("mcp_timeout", retryable=True) from exc
        except asyncio.CancelledError:
            await self.repository.complete_failure(
                context,
                execution.call_id,
                "mcp_cancelled",
                retryable=True,
            )
            raise
        except Exception as exc:
            await self.repository.complete_failure(
                context,
                execution.call_id,
                "mcp_call_error",
                retryable=True,
            )
            raise McpGatewayError("mcp_call_error", retryable=True) from exc

        try:
            completed = await self.repository.complete_success(
                context,
                execution.call_id,
                result,
            )
        except ToolContractError as exc:
            await self.repository.complete_failure(
                context,
                execution.call_id,
                "mcp_invalid_result",
                retryable=False,
            )
            raise McpGatewayError("mcp_invalid_result", retryable=False) from exc
        if not completed:
            raise McpGatewayError("tool_call_fenced", retryable=True)
        return result
