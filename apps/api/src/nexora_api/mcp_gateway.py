import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from nexora_api import metrics
from nexora_api.config import Settings
from nexora_api.run_state import ExecutionContext
from nexora_api.telemetry import record, record_error, span
from nexora_api.tool_contracts import ToolContractError
from nexora_api.tool_repository import ToolGovernanceRepository


class McpToolAdapter(Protocol):
    async def call_tool(
        self,
        remote_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> Any: ...


@dataclass
class McpAdapterError(Exception):
    code: str
    retryable: bool

    def __str__(self) -> str:
        return self.code


@dataclass
class ApprovalRequired(Exception):
    tool_call_id: UUID
    approval_id: UUID

    def __str__(self) -> str:
        return "tool_approval_required"


@dataclass
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
        # Spans and metrics record the governed decision, never the arguments or result.
        with span(
            "tool.invoke",
            **{
                "nexora.workspace_id": context.workspace_id,
                "nexora.run_id": context.run_id,
                "nexora.attempt": context.attempt_count,
                "nexora.tool_name": tool_name,
                "nexora.tool_call_key": call_key,
            },
        ) as active:
            return await self._invoke(context, call_key, tool_name, arguments, is_cancelled, active)

    async def _invoke(self, context, call_key, tool_name, arguments, is_cancelled, active):
        started = time.perf_counter()
        plan = await self.repository.prepare_call(context, call_key, tool_name, arguments)
        server_key = plan.tool.server_key
        record(
            active,
            **{
                "nexora.server_key": server_key,
                "nexora.side_effect": plan.tool.side_effect,
                "nexora.policy_action": plan.action,
            },
        )
        if plan.action == "replay":
            metrics.observe_tool_call(server_key, "replayed")
            return plan.result
        if plan.action == "deny":
            code = plan.error_code or "tool_denied"
            record_error(active, code)
            metrics.observe_tool_call(server_key, "denied")
            raise McpGatewayError(code, retryable=False)
        if plan.action == "approval":
            if plan.approval_id is None:
                raise RuntimeError("approval plan missing approval_id")
            record(active, **{"nexora.approval_id": plan.approval_id})
            metrics.observe_tool_call(server_key, "approval")
            raise ApprovalRequired(plan.call_id, plan.approval_id)

        if is_cancelled is not None and await is_cancelled():
            metrics.observe_tool_call(server_key, "cancelled")
            raise McpGatewayError("cancel_requested", retryable=False)

        try:
            execution = await self.repository.mark_running(plan.call_id, context)
        except ToolContractError as exc:
            record_error(active, exc.code)
            metrics.observe_tool_call(server_key, "error")
            raise McpGatewayError(exc.code, retryable=False) from exc

        adapter = self.adapters.get(execution.server_key)
        if adapter is None:
            await self.repository.complete_failure(
                execution.call_id, "mcp_server_unavailable", retryable=True, context=context
            )
            record_error(active, "mcp_server_unavailable")
            metrics.observe_tool_call(execution.server_key, "error")
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
        except McpAdapterError as exc:
            await self.repository.complete_failure(
                execution.call_id, exc.code, retryable=exc.retryable, context=context
            )
            record_error(active, exc.code)
            outcome = "error" if exc.retryable else "denied"
            metrics.observe_tool_call(
                execution.server_key, outcome, time.perf_counter() - started
            )
            raise McpGatewayError(exc.code, retryable=exc.retryable) from exc
        except TimeoutError as exc:
            await self.repository.complete_failure(
                execution.call_id, "mcp_timeout", retryable=True, context=context
            )
            record_error(active, "mcp_timeout")
            metrics.observe_tool_call(
                execution.server_key, "timeout", time.perf_counter() - started
            )
            raise McpGatewayError("mcp_timeout", retryable=True) from exc
        except asyncio.CancelledError:
            await self.repository.complete_failure(
                execution.call_id, "mcp_cancelled", retryable=True, context=context
            )
            metrics.observe_tool_call(
                execution.server_key, "cancelled", time.perf_counter() - started
            )
            raise
        except Exception as exc:
            await self.repository.complete_failure(
                execution.call_id, "mcp_call_error", retryable=True, context=context
            )
            record_error(active, "mcp_call_error")
            metrics.observe_tool_call(execution.server_key, "error", time.perf_counter() - started)
            raise McpGatewayError("mcp_call_error", retryable=True) from exc

        try:
            completed = await self.repository.complete_success(execution.call_id, result, context)
        except ToolContractError as exc:
            await self.repository.complete_failure(
                execution.call_id, "mcp_invalid_result", retryable=False, context=context
            )
            record_error(active, "mcp_invalid_result")
            metrics.observe_tool_call(execution.server_key, "error", time.perf_counter() - started)
            raise McpGatewayError("mcp_invalid_result", retryable=False) from exc
        if not completed:
            record_error(active, "tool_call_fenced")
            metrics.observe_tool_call(execution.server_key, "error", time.perf_counter() - started)
            raise McpGatewayError("tool_call_fenced", retryable=True)
        metrics.observe_tool_call(execution.server_key, "success", time.perf_counter() - started)
        return result
