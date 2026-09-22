"""MCP adapter backed by Nexora's tenant integration vault.

This adapter is deliberately context-aware: a model only names a governed tool. The
workspace, installed standard agent and its integration binding come from the durable run
context, so credentials and tenant-chosen endpoints never enter the prompt or tool
arguments.
"""

import asyncio
import json
from typing import Any

from nexora_api.config import Settings
from nexora_api.integration_repository import IntegrationRepository
from nexora_api.mcp_gateway import McpAdapterError
from nexora_api.run_state import ExecutionContext


class IntegrationMcpAdapter:
    """Execute a governed tool through the integration bound to the running agent."""

    requires_context = True

    def __init__(self, settings: Settings) -> None:
        self.repository = IntegrationRepository(settings)

    async def call_tool(
        self,
        remote_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
        *,
        context: ExecutionContext | None = None,
    ) -> Any:
        if context is None:
            raise McpAdapterError("integration_context_missing", retryable=False)
        if not remote_name or len(remote_name) > 128:
            raise McpAdapterError("integration_invalid_tool_name", retryable=False)

        try:
            result = await asyncio.wait_for(
                self.repository.invoke_bound(
                    context.workspace_id,
                    context.agent_id,
                    remote_name,
                    arguments,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise McpAdapterError("integration_timeout", retryable=True) from exc
        except Exception as exc:
            code = getattr(exc, "code", None)
            if isinstance(code, str):
                retryable = code in {
                    "timeout",
                    "transport_error",
                    "rate_limited",
                    "provider_error",
                }
                raise McpAdapterError(code, retryable=retryable) from exc
            raise

        if not result.ok:
            code = result.error_code or "integration_call_failed"
            retryable = (
                result.status_code is None
                or result.status_code == 429
                or (result.status_code is not None and result.status_code >= 500)
            )
            raise McpAdapterError(code, retryable=retryable)

        body: Any = None
        if result.body:
            try:
                body = json.loads(result.body)
            except json.JSONDecodeError:
                body = result.body
        return {
            "ok": True,
            "status_code": result.status_code,
            "data": body,
        }
