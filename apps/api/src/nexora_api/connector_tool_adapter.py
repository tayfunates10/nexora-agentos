"""Governed execution adapter for standard-agent connector tools.

The model sees a public manifest tool name. The run snapshot maps it to an immutable
connector contract and a specific tenant integration. Immediately before egress this
adapter proves that the tenant-agent binding still points at the same account, resolves
the credential only inside the worker process, and invokes only the snapshotted endpoint.
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException

from nexora_api.config import Settings
from nexora_api.connector_runtime import ConnectorError, ConnectorRuntime
from nexora_api.integration_manifest import load_connector
from nexora_api.integration_repository import IntegrationRepository
from nexora_api.mcp_gateway import McpAdapterError
from nexora_api.run_state import ExecutionContext

_RETRYABLE_ERRORS = {"timeout", "transport_error", "rate_limited", "provider_error"}


class ConnectorToolAdapter:
    """Execute one connector capability from an immutable standard-run snapshot."""

    def __init__(self, settings: Settings):
        self.integrations = IntegrationRepository(settings)
        self.runtime = ConnectorRuntime(settings.integration_request_timeout_seconds)

    @staticmethod
    def _spec(context: ExecutionContext, remote_name: str) -> dict[str, Any]:
        if context.agent_kind != "standard" or not isinstance(context.agent_snapshot, dict):
            raise McpAdapterError("connector_standard_run_required", retryable=False)
        tools = context.agent_snapshot.get("connector_tools")
        if not isinstance(tools, dict):
            raise McpAdapterError("connector_tool_snapshot_missing", retryable=False)
        spec = tools.get(remote_name)
        if not isinstance(spec, dict):
            raise McpAdapterError("connector_tool_not_snapshotted", retryable=False)
        return spec

    async def call_tool(
        self,
        context: ExecutionContext,
        remote_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> Any:
        del timeout_seconds  # The outer gateway timeout is the hard execution ceiling.
        spec = self._spec(context, remote_name)
        try:
            integration_id = UUID(str(spec["tenant_integration_id"]))
            binding_key = str(spec["binding_key"])
            definition_id = str(spec["definition_id"])
            capability = str(spec["capability"])
            definition = load_connector(spec["definition"])
        except (KeyError, TypeError, ValueError):
            raise McpAdapterError("connector_tool_snapshot_invalid", retryable=False) from None
        if definition.id != definition_id or definition.endpoint_for(capability) is None:
            raise McpAdapterError("connector_tool_snapshot_invalid", retryable=False)

        async with self.integrations.connection() as connection:
            # Approval may have taken minutes. Revalidate the exact account binding now;
            # never let a rebinding silently redirect an already-approved action.
            bound = await connection.execute(
                """SELECT tenant_integration_id
                   FROM agent_integration_bindings
                   WHERE workspace_id=%s AND tenant_agent_id=%s AND binding_key=%s
                   FOR SHARE""",
                (context.workspace_id, context.agent_id, binding_key),
            )
            binding = await bound.fetchone()
            if binding is None or binding["tenant_integration_id"] != integration_id:
                raise McpAdapterError("connector_binding_changed", retryable=False)

            try:
                current_definition, credential, config, row = await self.integrations.resolve(
                    connection, context.workspace_id, integration_id
                )
                if row["integration_definition_id"] != definition_id:
                    raise McpAdapterError("connector_binding_changed", retryable=False)
                credential = await self.integrations._refresh_if_needed(
                    connection,
                    context.workspace_id,
                    integration_id,
                    current_definition,
                    credential,
                )
            except McpAdapterError:
                raise
            except HTTPException:
                raise McpAdapterError("connector_binding_unusable", retryable=False) from None

            try:
                key = arguments.get("idempotency_key")
                result = await self.runtime.invoke(
                    definition,
                    capability,
                    credential,
                    config,
                    arguments=arguments,
                    idempotency_key=key if isinstance(key, str) else None,
                )
            except ConnectorError as exc:
                raise McpAdapterError("connector_" + exc.code, retryable=False) from exc

        if result.ok:
            return {"status_code": result.status_code, "body": result.body}

        endpoint = definition.endpoint_for(capability)
        retryable = bool(
            endpoint
            and endpoint.retry in ("safe", "idempotent")
            and result.error_code in _RETRYABLE_ERRORS
        )
        raise McpAdapterError(
            "connector_" + (result.error_code or "provider_error"),
            retryable=retryable,
        )
