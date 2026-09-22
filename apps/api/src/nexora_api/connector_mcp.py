"""Bridge tenant integrations into the governed MCP tool boundary.

Connected provider endpoints become workspace tools. The tool records contain only an
integration UUID and capability; credentials stay sealed in the IntegrationRepository
and are opened only after the gateway has authorized a call for the current workspace.
"""

import json
from uuid import UUID

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.connector_runtime import ConnectorError
from nexora_api.integration_manifest import load_connector
from nexora_api.mcp_gateway import McpAdapterError
from nexora_api.workspace_repository import WorkspaceRepository

CONNECTOR_SERVER_KEY = "connector"


class ConnectorToolProvisioner:
    """Materialize executable endpoints as safe workspace tools.

    This is a consequence of an already-authorized integration connect operation, not a
    second user-managed tool registration. Existing DENY decisions are never loosened;
    a connector endpoint that becomes mutating is always upgraded to approval-required.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.workspaces = WorkspaceRepository(settings)

    async def sync(self, principal: Principal, integration, request_id: str) -> list[str]:
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ) as connection:
            definition_result = await connection.execute(
                "SELECT manifest FROM integration_definitions WHERE id=%s",
                (integration.integration_definition_id,),
            )
            row = await definition_result.fetchone()
            if row is None:
                raise HTTPException(404, "Unknown integration")
            manifest = load_connector(row["manifest"])
            names: list[str] = []
            for endpoint in manifest.endpoints:
                name = f"{manifest.id}.{endpoint.capability}"
                tool = await connection.execute(
                    """INSERT INTO tool_definitions
                       (id,workspace_id,name,server_key,remote_name,description,input_schema,
                        output_schema,side_effect,enabled,created_by_issuer,created_by_subject)
                       VALUES (gen_random_uuid(),%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s)
                       ON CONFLICT (workspace_id,name) DO UPDATE SET
                         server_key=EXCLUDED.server_key,
                         remote_name=EXCLUDED.remote_name,
                         description=EXCLUDED.description,
                         input_schema=EXCLUDED.input_schema,
                         output_schema=EXCLUDED.output_schema,
                         side_effect=EXCLUDED.side_effect,
                         enabled=true,
                         updated_at=now()
                       RETURNING id""",
                    (
                        integration.workspace_id,
                        name,
                        CONNECTOR_SERVER_KEY,
                        f"{integration.id}:{endpoint.capability}",
                        endpoint.description,
                        Jsonb(endpoint.input_schema),
                        (
                            Jsonb(endpoint.output_schema)
                            if endpoint.output_schema is not None
                            else None
                        ),
                        endpoint.side_effect,
                        principal.issuer,
                        principal.subject,
                    ),
                )
                tool_id = (await tool.fetchone())["id"]
                default_decision = "allow" if endpoint.side_effect == "read" else "require_approval"
                default_reason = (
                    "Connector read is allowed"
                    if endpoint.side_effect == "read"
                    else "Connector mutation requires human approval"
                )
                await connection.execute(
                    """INSERT INTO tool_policies
                       (workspace_id,tool_id,decision,reason,updated_by_issuer,updated_by_subject)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (workspace_id,tool_id) DO UPDATE SET
                         decision=CASE
                           WHEN tool_policies.decision='deny' THEN 'deny'
                           WHEN EXCLUDED.decision='require_approval' THEN 'require_approval'
                           ELSE tool_policies.decision
                         END,
                         reason=CASE
                           WHEN tool_policies.decision='deny' THEN tool_policies.reason
                           WHEN EXCLUDED.decision='require_approval' THEN EXCLUDED.reason
                           ELSE tool_policies.reason
                         END,
                         updated_at=CASE
                           WHEN EXCLUDED.decision='require_approval' THEN now()
                           ELSE tool_policies.updated_at
                         END""",
                    (
                        integration.workspace_id,
                        tool_id,
                        default_decision,
                        default_reason,
                        principal.issuer,
                        principal.subject,
                    ),
                )
                names.append(name)
            await self.workspaces.audit(
                connection,
                principal,
                integration.workspace_id,
                "integration.tools_synced",
                request_id,
                str(integration.id),
            )
            return names

    async def disable(self, workspace_id: UUID, integration_id: UUID) -> None:
        """Hide tools whose target connection has been removed."""
        prefix = f"{integration_id}:%"
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ) as connection:
            await connection.execute(
                """UPDATE tool_definitions
                   SET enabled=false,updated_at=now()
                   WHERE workspace_id=%s AND server_key=%s AND remote_name LIKE %s""",
                (workspace_id, CONNECTOR_SERVER_KEY, prefix),
            )

class ConnectorMcpAdapter:
    """Context-aware adapter for integrations stored in the tenant vault."""

    def __init__(self, settings: Settings):
        # Local import avoids a cycle: integration_repository owns the Integration API
        # contracts, which import this module only for post-connect tool provisioning.
        from nexora_api.integration_repository import IntegrationRepository

        self.repository = IntegrationRepository(settings)

    @staticmethod
    def _target(remote_name: str) -> tuple[UUID, str]:
        try:
            raw_id, capability = remote_name.split(":", 1)
            integration_id = UUID(raw_id)
        except (ValueError, AttributeError):
            raise McpAdapterError("connector_target_invalid", retryable=False) from None
        if not capability:
            raise McpAdapterError("connector_target_invalid", retryable=False)
        return integration_id, capability

    async def call_tool_for_context(
        self,
        context,
        remote_name: str,
        arguments: dict[str, object],
        timeout_seconds: float,
    ):
        integration_id, capability = self._target(remote_name)
        try:
            async with self.repository.connection() as connection:
                definition, document, config, _ = await self.repository.resolve(
                    connection, context.workspace_id, integration_id
                )
                document = await self.repository._refresh_if_needed(
                    connection,
                    context.workspace_id,
                    integration_id,
                    definition,
                    document,
                )
                result = await self.repository.runtime.invoke(
                    definition, capability, document, config, arguments
                )
        except HTTPException as exc:
            code = "connector_not_found" if exc.status_code == 404 else "connector_unavailable"
            raise McpAdapterError(code, retryable=False) from None
        except ConnectorError as exc:
            retryable = exc.code in {"host_unresolvable"}
            raise McpAdapterError(exc.code, retryable=retryable) from exc

        if not result.ok:
            code = "connector_" + (result.error_code or "provider_error")
            retryable = result.error_code in {
                "timeout",
                "transport_error",
                "rate_limited",
            } or (result.status_code is not None and result.status_code >= 500)
            raise McpAdapterError(code, retryable=retryable)

        if not result.body:
            return {"ok": True, "status_code": result.status_code}
        body = result.body
        truncated = len(body.encode("utf-8")) > 48_000
        if truncated:
            body = body[:48_000]
        try:
            data = body if truncated else json.loads(body)
        except json.JSONDecodeError:
            data = body
        return {
            "ok": True,
            "status_code": result.status_code,
            "data": data,
            "truncated": truncated,
        }
