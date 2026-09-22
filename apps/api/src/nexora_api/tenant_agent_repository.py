"""Persistence for tenant agent instances: installs, bindings, version moves and forks.

Three properties are load-bearing here.

*A tenant's configuration outlives every version move.* Settings, prompt overrides,
bindings and display names live on the instance, not on the version it happens to be
pinned to, so updating and rolling back never touch them.

*An agent never starts half-configured.* An instance is only active once every integration
its manifest requires is bound to a connected account in the same workspace.

*A fork is a copy, not a link.* Forking writes the manifest's text into a workspace agent
and records where it came from. A later catalog release cannot change a fork's behaviour.
"""

import hashlib
import json
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from nexora_api.agent_catalog import Channel
from nexora_api.agent_catalog_repository import offered_version
from nexora_api.agent_manifest import AgentManifest, Version
from nexora_api.agents import AgentRun
from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.integration_manifest import load_connector
from nexora_api.integrations import IntegrationStatus
from nexora_api.tenant_agents import (
    AgentBinding,
    CatalogEntry,
    ForkedAgent,
    ForkInput,
    InstallInput,
    InstanceStatus,
    IntegrationRequirement,
    Readiness,
    TenantAgent,
    TenantAgentRunInput,
    TenantAgentSummary,
    TenantAgentUpdate,
    UpdateMode,
    UpdatePolicy,
    UpdatePolicyInput,
    VersionEvent,
)
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission

INSTANCE_COLUMNS = """t.id,t.workspace_id,t.catalog_agent_id,t.version_id,t.display_name,t.status,
                      t.paused_reason,t.update_channel,t.update_mode,t.instructions_override,
                      t.settings,t.created_at,t.updated_at"""


class TenantAgentRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.workspaces = WorkspaceRepository(settings)
        self.runtime_version = Version(settings.agent_runtime_version)

    def connection(self):
        return self.workspaces.connection()

    # ------------------------------------------------------------------ catalog view
    async def browse_catalog(
        self, principal: Principal, workspace_id: UUID, limit: int, cursor: str | None
    ) -> list[CatalogEntry]:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            channel = await self._channel(connection, workspace_id)
            # Public entries are offered to everyone; restricted ones only where the
            # platform granted this workspace an entitlement.
            result = await connection.execute(
                """SELECT a.id,a.slug,a.name,a.description,a.category,a.icon,a.status
                   FROM catalog_agents a
                   WHERE a.status NOT IN ('draft','disabled')
                     AND (a.visibility='public' OR EXISTS (
                           SELECT 1 FROM catalog_agent_entitlements e
                           WHERE e.catalog_agent_id=a.id AND e.workspace_id=%s))
                     AND (%s::text IS NULL OR a.slug > %s::text)
                   ORDER BY a.slug LIMIT %s""",
                (workspace_id, cursor, cursor, limit),
            )
            entries = []
            for row in await result.fetchall():
                version_id, version = await offered_version(
                    connection, row["id"], workspace_id, channel
                )
                manifest = await self._manifest(connection, version_id) if version_id else None
                installed = await connection.execute(
                    """SELECT count(*) AS installed FROM tenant_agents
                       WHERE workspace_id=%s AND catalog_agent_id=%s""",
                    (workspace_id, row["id"]),
                )
                entries.append(
                    CatalogEntry(
                        catalog_agent_id=row["id"],
                        slug=row["slug"],
                        name=row["name"],
                        description=row["description"],
                        category=row["category"],
                        icon=row["icon"],
                        status=row["status"],
                        available_version=version,
                        installed_count=(await installed.fetchone())["installed"],
                        required_integrations=manifest.required_integrations if manifest else [],
                        optional_integrations=manifest.optional_integrations if manifest else [],
                        capabilities=manifest.capabilities if manifest else [],
                        approval_required=manifest.approval_required if manifest else [],
                    )
                )
            return entries

    async def _manifest(self, connection, version_id: UUID) -> AgentManifest:
        result = await connection.execute(
            "SELECT manifest FROM catalog_agent_versions WHERE id=%s", (version_id,)
        )
        row = await result.fetchone()
        if row is None:
            raise HTTPException(404)
        return AgentManifest.model_validate(row["manifest"])

    async def _channel(self, connection, workspace_id: UUID) -> Channel:
        result = await connection.execute(
            "SELECT channel FROM agent_update_policies WHERE workspace_id=%s", (workspace_id,)
        )
        row = await result.fetchone()
        return Channel(row["channel"]) if row else Channel.STABLE

    # ------------------------------------------------------------------- readiness
    async def _bindings(self, connection, workspace_id: UUID, agent_id: UUID):
        result = await connection.execute(
            """SELECT b.binding_key,b.tenant_integration_id,b.created_at,b.updated_at,
                      i.display_name,i.status
               FROM agent_integration_bindings b
               JOIN tenant_integrations i ON i.id=b.tenant_integration_id
               WHERE b.tenant_agent_id=%s AND b.workspace_id=%s
               ORDER BY b.binding_key""",
            (agent_id, workspace_id),
        )
        return [
            AgentBinding(
                binding_key=row["binding_key"],
                tenant_integration_id=row["tenant_integration_id"],
                display_name=row["display_name"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in await result.fetchall()
        ]

    @staticmethod
    def _readiness(manifest: AgentManifest, bindings: list[AgentBinding]) -> Readiness:
        """An agent is ready only when every required integration resolves to a live account."""
        by_key = {binding.binding_key: binding for binding in bindings}
        requirements = []
        missing = []
        for name, required in manifest.integrations():
            binding = by_key.get(name)
            satisfied = binding is not None and binding.status == IntegrationStatus.CONNECTED
            if required and not satisfied:
                missing.append(name)
            requirements.append(
                IntegrationRequirement(
                    integration_definition_id=name,
                    name=name,
                    required=required,
                    bound_integration_id=binding.tenant_integration_id if binding else None,
                    bound_display_name=binding.display_name if binding else None,
                    bound_status=binding.status if binding else None,
                    satisfied=satisfied,
                )
            )
        return Readiness(ready=not missing, requirements=requirements, missing_required=missing)

    # ---------------------------------------------------------------- instance reads
    async def _row(self, connection, workspace_id: UUID, agent_id: UUID, lock: bool = False):
        result = await connection.execute(
            f"""SELECT {INSTANCE_COLUMNS},a.slug,v.version
                FROM tenant_agents t
                JOIN catalog_agents a ON a.id=t.catalog_agent_id
                JOIN catalog_agent_versions v ON v.id=t.version_id
                WHERE t.workspace_id=%s AND t.id=%s"""
            + (" FOR UPDATE OF t" if lock else ""),
            (workspace_id, agent_id),
        )
        row = await result.fetchone()
        if row is None:
            raise HTTPException(404)
        return row

    async def _view(self, connection, workspace_id: UUID, row) -> TenantAgent:
        manifest = await self._manifest(connection, row["version_id"])
        bindings = await self._bindings(connection, workspace_id, row["id"])
        channel = Channel(row["update_channel"])
        _, available = await offered_version(
            connection, row["catalog_agent_id"], workspace_id, channel
        )
        return TenantAgent(
            id=row["id"],
            workspace_id=row["workspace_id"],
            catalog_agent_id=row["catalog_agent_id"],
            slug=row["slug"],
            display_name=row["display_name"],
            version=row["version"],
            available_version=available,
            status=row["status"],
            update_channel=channel,
            update_mode=row["update_mode"],
            instructions_override=row["instructions_override"],
            settings=row["settings"],
            manifest=manifest,
            bindings=bindings,
            readiness=self._readiness(manifest, bindings),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def get(self, principal: Principal, workspace_id: UUID, agent_id: UUID) -> TenantAgent:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            return await self._view(
                connection, workspace_id, await self._row(connection, workspace_id, agent_id)
            )

    @staticmethod
    def _run(row) -> AgentRun:
        return AgentRun(
            id=row["id"],
            workspace_id=row["workspace_id"],
            agent_id=row["tenant_agent_id"],
            trace_id=row["trace_id"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            cancel_requested_at=row["cancel_requested_at"],
            finished_at=row["finished_at"],
            failure_code=row["failure_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def _connector_run_contracts(
        self,
        connection,
        principal: Principal,
        workspace_id: UUID,
        manifest: AgentManifest,
        bindings: list[AgentBinding],
        settings: dict[str, object],
    ) -> tuple[
        list[str],
        dict[str, str],
        dict[str, dict[str, object]],
        dict[str, str] | None,
    ]:
        """Freeze connector contracts and register immutable governed tool aliases."""
        by_key = {binding.binding_key: binding for binding in bindings}
        declared_integrations = set(manifest.required_integrations) | set(
            manifest.optional_integrations
        )
        required_tools = set(manifest.required_tools)
        allowed_tools: list[str] = []
        aliases: dict[str, str] = {}
        connector_tools: dict[str, dict[str, object]] = {}
        browser_snapshot: dict[str, str] | None = None

        for public_name in [*manifest.required_tools, *manifest.optional_tools]:
            if public_name in {"nexora.tasks.create", "nexora.tasks.verify"}:
                if public_name == "nexora.tasks.create":
                    remote_name = "child.create"
                    description = (
                        "Create a durable follow-up task inside this run without "
                        "performing an external action."
                    )
                    input_schema = {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": 300},
                            "description": {"type": "string", "maxLength": 2000},
                            "action_tool": {"type": "string", "minLength": 2, "maxLength": 64},
                            "depends_on": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "minLength": 36,
                                    "maxLength": 36,
                                },
                                "maxItems": 20,
                            },
                            "idempotency_key": {
                                "type": "string",
                                "minLength": 8,
                                "maxLength": 128,
                            },
                        },
                        "required": ["title", "idempotency_key"],
                        "additionalProperties": False,
                    }
                else:
                    remote_name = "task.verify"
                    description = (
                        "Complete a follow-up only after a successful governed action "
                        "and later read evidence."
                    )
                    input_schema = {
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "minLength": 36,
                                "maxLength": 36,
                            },
                            "verification_tool": {
                                "type": "string",
                                "minLength": 2,
                                "maxLength": 64,
                            },
                            "summary": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                            },
                            "satisfied": {"type": "boolean"},
                            "idempotency_key": {
                                "type": "string",
                                "minLength": 8,
                                "maxLength": 128,
                            },
                        },
                        "required": [
                            "task_id",
                            "verification_tool",
                            "summary",
                            "satisfied",
                            "idempotency_key",
                        ],
                        "additionalProperties": False,
                    }
                digest = hashlib.sha256(
                    json.dumps(
                        {"public_name": public_name, "input_schema": input_schema},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                internal_name = f"tasks.{digest[:48]}"
                await connection.execute(
                    """INSERT INTO tool_definitions
                       (id,workspace_id,name,server_key,remote_name,description,input_schema,
                        output_schema,side_effect,enabled,created_by_issuer,created_by_subject)
                       VALUES (%s,%s,%s,'tasks',%s,%s,%s,NULL,'write',true,%s,%s)
                       ON CONFLICT (workspace_id,name) DO NOTHING""",
                    (
                        uuid4(),
                        workspace_id,
                        internal_name,
                        remote_name,
                        description,
                        Jsonb(input_schema),
                        principal.issuer,
                        principal.subject,
                    ),
                )
                stored_result = await connection.execute(
                    """SELECT id,server_key,remote_name,input_schema,side_effect,enabled
                       FROM tool_definitions WHERE workspace_id=%s AND name=%s""",
                    (workspace_id, internal_name),
                )
                stored = await stored_result.fetchone()
                if (
                    stored is None
                    or stored["server_key"] != "tasks"
                    or stored["remote_name"] != remote_name
                    or stored["input_schema"] != input_schema
                    or stored["side_effect"] != "write"
                ):
                    raise HTTPException(409, "Task tool contract collision")
                if not stored["enabled"]:
                    if public_name in required_tools:
                        raise HTTPException(409, "Required task capability is disabled")
                    continue
                await connection.execute(
                    """INSERT INTO tool_policies
                       (workspace_id,tool_id,decision,reason,updated_by_issuer,updated_by_subject)
                       VALUES (%s,%s,'allow','Internal run task metadata only',%s,%s)
                       ON CONFLICT (workspace_id,tool_id) DO NOTHING""",
                    (
                        workspace_id,
                        stored["id"],
                        principal.issuer,
                        principal.subject,
                    ),
                )
                allowed_tools.append(public_name)
                aliases[public_name] = internal_name
                continue

            if public_name == "browser.page.inspect":
                configured = (
                    self.settings.browser_runtime_url is not None
                    and self.settings.browser_runtime_token is not None
                )
                site_url = settings.get("site_url")
                if not configured or not isinstance(site_url, str):
                    if public_name in required_tools:
                        raise HTTPException(409, "Required browser capability is unavailable")
                    continue
                parsed = urlsplit(site_url)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    if public_name in required_tools:
                        raise HTTPException(409, "Browser site_url must be a public HTTPS site")
                    continue
                allowed_origin = f"{parsed.scheme}://{parsed.netloc}"
                input_schema = {
                    "type": "object",
                    "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 1000}},
                    "required": ["path"],
                    "additionalProperties": False,
                }
                digest = hashlib.sha256(
                    json.dumps(
                        {"public_name": public_name, "allowed_origin": allowed_origin},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                internal_name = f"browser.{digest[:48]}"
                await connection.execute(
                    """INSERT INTO tool_definitions
                       (id,workspace_id,name,server_key,remote_name,description,input_schema,
                        output_schema,side_effect,enabled,created_by_issuer,created_by_subject)
                       VALUES (%s,%s,%s,'browser','page.inspect',%s,%s,NULL,'read',true,%s,%s)
                       ON CONFLICT (workspace_id,name) DO NOTHING""",
                    (
                        uuid4(),
                        workspace_id,
                        internal_name,
                        "Render and inspect an allowlisted customer web page.",
                        Jsonb(input_schema),
                        principal.issuer,
                        principal.subject,
                    ),
                )
                stored_result = await connection.execute(
                    """SELECT id,server_key,remote_name,input_schema,side_effect,enabled
                       FROM tool_definitions
                       WHERE workspace_id=%s AND name=%s""",
                    (workspace_id, internal_name),
                )
                stored = await stored_result.fetchone()
                if (
                    stored is None
                    or stored["server_key"] != "browser"
                    or stored["remote_name"] != "page.inspect"
                    or stored["input_schema"] != input_schema
                    or stored["side_effect"] != "read"
                ):
                    raise HTTPException(409, "Browser tool contract collision")
                if not stored["enabled"]:
                    if public_name in required_tools:
                        raise HTTPException(409, "Required browser capability is disabled")
                    continue
                await connection.execute(
                    """INSERT INTO tool_policies
                       (workspace_id,tool_id,decision,reason,updated_by_issuer,updated_by_subject)
                       VALUES (%s,%s,'allow','Run-scoped allowlisted browser inspection',%s,%s)
                       ON CONFLICT (workspace_id,tool_id) DO NOTHING""",
                    (workspace_id, stored["id"], principal.issuer, principal.subject),
                )
                allowed_tools.append(public_name)
                aliases[public_name] = internal_name
                browser_snapshot = {"allowed_origin": allowed_origin}
                continue

            integration_key, separator, capability = public_name.partition(".")
            if not separator or integration_key not in declared_integrations:
                allowed_tools.append(public_name)
                continue

            binding = by_key.get(integration_key)
            if binding is None or binding.status != IntegrationStatus.CONNECTED:
                if public_name in required_tools:
                    raise HTTPException(409, f"Required tool is not connected: {public_name}")
                continue

            result = await connection.execute(
                """SELECT t.integration_definition_id,t.status,t.auth_type,
                          t.credential_reference,t.config,d.manifest
                   FROM tenant_integrations t
                   JOIN integration_definitions d ON d.id=t.integration_definition_id
                   WHERE t.workspace_id=%s AND t.id=%s
                   FOR SHARE OF t""",
                (workspace_id, binding.tenant_integration_id),
            )
            integration = await result.fetchone()
            if (
                integration is None
                or integration["integration_definition_id"] != integration_key
                or integration["status"] != IntegrationStatus.CONNECTED
            ):
                if public_name in required_tools:
                    raise HTTPException(409, f"Required tool is unavailable: {public_name}")
                continue

            definition = load_connector(integration["manifest"])
            endpoint = definition.endpoint_for(capability)
            if endpoint is None:
                if public_name in required_tools:
                    raise HTTPException(
                        409, f"Required connector capability is not executable: {public_name}"
                    )
                continue

            definition_document = definition.model_dump(mode="json")
            config_fingerprint = hashlib.sha256(
                json.dumps(
                    integration["config"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            credential_reference = (
                str(integration["credential_reference"])
                if integration["auth_type"] != "oauth2"
                and integration["credential_reference"] is not None
                else None
            )
            contract_document = {
                "public_name": public_name,
                "definition_id": definition.id,
                "definition_version": definition.version,
                "endpoint": endpoint.model_dump(mode="json"),
                "tenant_integration_id": str(binding.tenant_integration_id),
                "config_fingerprint": config_fingerprint,
                "credential_reference": credential_reference,
            }
            digest = hashlib.sha256(
                json.dumps(
                    contract_document,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            internal_name = f"connector.{digest[:48]}"
            tool_id = uuid4()
            await connection.execute(
                """INSERT INTO tool_definitions
                   (id,workspace_id,name,server_key,remote_name,description,input_schema,
                    output_schema,side_effect,enabled,created_by_issuer,created_by_subject)
                   VALUES (%s,%s,%s,'connector',%s,%s,%s,%s,%s,true,%s,%s)
                   ON CONFLICT (workspace_id,name) DO NOTHING""",
                (
                    tool_id,
                    workspace_id,
                    internal_name,
                    f"{binding.tenant_integration_id}:{capability}",
                    endpoint.description,
                    Jsonb(endpoint.input_schema),
                    Jsonb(endpoint.output_schema) if endpoint.output_schema is not None else None,
                    endpoint.side_effect,
                    principal.issuer,
                    principal.subject,
                ),
            )
            stored_result = await connection.execute(
                """SELECT id,server_key,remote_name,input_schema,output_schema,side_effect,enabled
                   FROM tool_definitions
                   WHERE workspace_id=%s AND name=%s""",
                (workspace_id, internal_name),
            )
            stored = await stored_result.fetchone()
            if (
                stored is None
                or stored["server_key"] != "connector"
                or stored["remote_name"] != f"{binding.tenant_integration_id}:{capability}"
                or stored["input_schema"] != endpoint.input_schema
                or stored["output_schema"] != endpoint.output_schema
                or stored["side_effect"] != endpoint.side_effect
            ):
                raise HTTPException(409, "Connector tool contract collision")
            if not stored["enabled"]:
                if public_name in required_tools:
                    raise HTTPException(409, f"Required tool is disabled: {public_name}")
                continue

            decision = "allow" if endpoint.side_effect == "read" else "require_approval"
            reason = (
                "Standard connector read capability"
                if endpoint.side_effect == "read"
                else "Standard connector side effect requires approval"
            )
            await connection.execute(
                """INSERT INTO tool_policies
                   (workspace_id,tool_id,decision,reason,updated_by_issuer,updated_by_subject)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (workspace_id,tool_id) DO NOTHING""",
                (
                    workspace_id,
                    stored["id"],
                    decision,
                    reason,
                    principal.issuer,
                    principal.subject,
                ),
            )

            allowed_tools.append(public_name)
            aliases[public_name] = internal_name
            connector_tools[public_name] = {
                "binding_key": integration_key,
                "tenant_integration_id": str(binding.tenant_integration_id),
                "definition_id": definition.id,
                "capability": capability,
                "config_fingerprint": config_fingerprint,
                "credential_reference": credential_reference,
                "definition": definition_document,
            }

        return allowed_tools, aliases, connector_tools, browser_snapshot

    async def create_run(
        self,
        principal: Principal,
        workspace_id: UUID,
        agent_id: UUID,
        body: TenantAgentRunInput,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[AgentRun, bool]:
        """Queue one installed standard agent without turning it into a custom fork."""
        fingerprint = hashlib.sha256(
            json.dumps(
                {"tenant_agent_id": str(agent_id), "input": body.input},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.RUN_AGENTS)
            row = await self._row(connection, workspace_id, agent_id, lock=True)
            manifest = await self._manifest(connection, row["version_id"])
            bindings = await self._bindings(connection, workspace_id, agent_id)
            readiness = self._readiness(manifest, bindings)
            if row["status"] != InstanceStatus.ACTIVE or not readiness.ready:
                raise HTTPException(409, "This standard agent is not ready to run")

            (
                allowed_tools,
                tool_aliases,
                connector_tools,
                browser_snapshot,
            ) = await self._connector_run_contracts(
                connection,
                principal,
                workspace_id,
                manifest,
                bindings,
                row["settings"],
            )
            snapshot = {
                "kind": "standard",
                "tenant_agent_id": str(agent_id),
                "catalog_agent_id": str(row["catalog_agent_id"]),
                "version_id": str(row["version_id"]),
                "version": row["version"],
                "instructions": row["instructions_override"] or manifest.system_instructions,
                "model_profile": manifest.model_policy.primary,
                "settings": row["settings"],
                "allowed_tools": allowed_tools,
                "tool_aliases": tool_aliases,
                "connector_tools": connector_tools,
                "browser": browser_snapshot,
                "manifest": manifest.model_dump(mode="json"),
                "bindings": {
                    binding.binding_key: {
                        "tenant_integration_id": str(binding.tenant_integration_id),
                        "status": binding.status.value,
                    }
                    for binding in bindings
                },
            }

            existing_result = await connection.execute(
                """SELECT id,workspace_id,tenant_agent_id,trace_id,status,attempt_count,
                          cancel_requested_at,finished_at,failure_code,created_at,updated_at,
                          request_hash
                   FROM agent_runs
                   WHERE workspace_id=%s AND requested_by_issuer=%s
                     AND requested_by_subject=%s AND idempotency_key=%s
                   FOR UPDATE""",
                (workspace_id, principal.issuer, principal.subject, idempotency_key),
            )
            existing = await existing_result.fetchone()
            if existing:
                if existing["request_hash"] != fingerprint:
                    raise HTTPException(409)
                if existing["tenant_agent_id"] is None:
                    raise HTTPException(409)
                return self._run(existing), False

            run_id = uuid4()
            trace_id = uuid4()
            result = await connection.execute(
                """INSERT INTO agent_runs
                   (id,workspace_id,tenant_agent_id,agent_snapshot,requested_by_issuer,
                    requested_by_subject,input_text,request_hash,idempotency_key,trace_id,status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued')
                   RETURNING id,workspace_id,tenant_agent_id,trace_id,status,attempt_count,
                             cancel_requested_at,finished_at,failure_code,created_at,updated_at""",
                (
                    run_id,
                    workspace_id,
                    agent_id,
                    Jsonb(snapshot),
                    principal.issuer,
                    principal.subject,
                    body.input,
                    fingerprint,
                    idempotency_key,
                    trace_id,
                ),
            )
            created = await result.fetchone()
            await connection.execute(
                """INSERT INTO run_tasks
                   (id,workspace_id,run_id,parent_task_id,kind,title,description,status,
                    action_tool_name,verification_state,idempotency_key,request_hash)
                   VALUES (%s,%s,%s,NULL,'goal','Run goal',%s,'planned',
                           NULL,'not_required',%s,%s)""",
                (
                    run_id,
                    workspace_id,
                    run_id,
                    body.input[:2000],
                    f"goal:{run_id}",
                    fingerprint,
                ),
            )
            await connection.execute(
                """INSERT INTO agent_run_events
                   (id,workspace_id,run_id,event_no,event_type,payload)
                   VALUES (%s,%s,%s,1,'run.queued',%s)""",
                (
                    uuid4(),
                    workspace_id,
                    run_id,
                    Jsonb({"request_id": request_id, "agent_kind": "standard"}),
                ),
            )
            await connection.execute(
                """INSERT INTO job_outbox (id,workspace_id,run_id,topic,payload)
                   VALUES (%s,%s,%s,'agent.run.queued.v1',%s)""",
                (
                    uuid4(),
                    workspace_id,
                    run_id,
                    Jsonb(
                        {
                            "run_id": str(run_id),
                            "workspace_id": str(workspace_id),
                            "agent_id": str(agent_id),
                            "trace_id": str(trace_id),
                            "agent_kind": "standard",
                        }
                    ),
                ),
            )
            await self.workspaces.audit(
                connection, principal, workspace_id, "tenant_agent_run.created", request_id
            )
            return self._run(created), True

    async def list_instances(
        self, principal: Principal, workspace_id: UUID, limit: int, cursor: UUID | None
    ) -> list[TenantAgentSummary]:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            result = await connection.execute(
                f"""SELECT {INSTANCE_COLUMNS},a.slug,v.version,v.manifest
                    FROM tenant_agents t
                    JOIN catalog_agents a ON a.id=t.catalog_agent_id
                    JOIN catalog_agent_versions v ON v.id=t.version_id
                    WHERE t.workspace_id=%s AND (%s::uuid IS NULL OR t.id > %s::uuid)
                    ORDER BY t.id LIMIT %s""",
                (workspace_id, cursor, cursor, limit),
            )
            summaries = []
            for row in await result.fetchall():
                manifest = AgentManifest.model_validate(row["manifest"])
                bindings = await self._bindings(connection, workspace_id, row["id"])
                _, available = await offered_version(
                    connection, row["catalog_agent_id"], workspace_id, row["update_channel"]
                )
                summaries.append(
                    TenantAgentSummary(
                        id=row["id"],
                        workspace_id=row["workspace_id"],
                        catalog_agent_id=row["catalog_agent_id"],
                        slug=row["slug"],
                        display_name=row["display_name"],
                        version=row["version"],
                        available_version=available,
                        status=row["status"],
                        update_channel=row["update_channel"],
                        update_mode=row["update_mode"],
                        ready=self._readiness(manifest, bindings).ready,
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
            return summaries

    # ------------------------------------------------------------------- installing
    async def _entitled_agent(self, connection, workspace_id: UUID, slug: str):
        result = await connection.execute(
            """SELECT a.id,a.slug,a.name FROM catalog_agents a
               WHERE a.slug=%s AND a.status NOT IN ('draft','disabled')
                 AND (a.visibility='public' OR EXISTS (
                       SELECT 1 FROM catalog_agent_entitlements e
                       WHERE e.catalog_agent_id=a.id AND e.workspace_id=%s))""",
            (slug, workspace_id),
        )
        row = await result.fetchone()
        if row is None:
            raise HTTPException(404)
        return row

    async def install(
        self, principal: Principal, workspace_id: UUID, body: InstallInput, request_id: str
    ) -> TenantAgent:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_AGENTS
            )
            agent = await self._entitled_agent(connection, workspace_id, body.slug)
            policy_channel = await self._channel(connection, workspace_id)
            channel = body.update_channel or policy_channel
            version_id, version = await offered_version(
                connection, agent["id"], workspace_id, channel
            )
            if version_id is None:
                raise HTTPException(409, "No released version is available on this channel")
            manifest = await self._manifest(connection, version_id)
            if not self.runtime_version.satisfies_minimum(manifest.runtime_minimum):
                raise HTTPException(
                    409, "This agent version requires a newer Nexora runtime than this deployment"
                )

            agent_id = uuid4()
            try:
                await connection.execute(
                    """INSERT INTO tenant_agents
                       (id,workspace_id,catalog_agent_id,version_id,display_name,status,
                        paused_reason,update_channel,update_mode)
                       VALUES (%s,%s,%s,%s,%s,'paused','not_ready',%s,%s)""",
                    (
                        agent_id,
                        workspace_id,
                        agent["id"],
                        version_id,
                        body.display_name or agent["name"],
                        channel,
                        body.update_mode or await self._mode(connection, workspace_id),
                    ),
                )
            except psycopg.errors.UniqueViolation:
                raise HTTPException(
                    409, "This workspace already has an instance with that name"
                ) from None

            for key, integration_id in body.bindings.items():
                await self._write_binding(
                    connection, workspace_id, agent_id, manifest, key, integration_id
                )
            if body.bindings:
                await self.workspaces.audit(
                    connection, principal, workspace_id, "agent.binding_changed", request_id
                )
            await self._record_version(
                connection,
                principal,
                workspace_id,
                agent_id,
                None,
                version_id,
                "installed",
                request_id,
            )
            # An instance starts paused and becomes active the moment its required
            # integrations resolve, so it never runs against a connection it lacks.
            await self._settle_status(connection, workspace_id, agent_id, manifest)
            await self.workspaces.audit(
                connection, principal, workspace_id, "agent.installed", request_id
            )
            return await self._view(
                connection, workspace_id, await self._row(connection, workspace_id, agent_id)
            )

    async def _mode(self, connection, workspace_id: UUID) -> str:
        result = await connection.execute(
            "SELECT mode FROM agent_update_policies WHERE workspace_id=%s", (workspace_id,)
        )
        row = await result.fetchone()
        return row["mode"] if row else UpdateMode.MANUAL

    async def _settle_status(
        self, connection, workspace_id: UUID, agent_id: UUID, manifest: AgentManifest
    ) -> None:
        """Keep the instance's status honest about whether it can actually run.

        An agent whose required connections are missing is paused, and resumes when they
        arrive. A pause someone asked for is a decision, so it is never undone by a
        binding change, an update or a rollback.
        """
        current = await connection.execute(
            "SELECT status,paused_reason FROM tenant_agents WHERE id=%s AND workspace_id=%s",
            (agent_id, workspace_id),
        )
        row = await current.fetchone()
        if row["status"] == InstanceStatus.DISABLED:
            return
        bindings = await self._bindings(connection, workspace_id, agent_id)
        ready = self._readiness(manifest, bindings).ready
        if ready and row["paused_reason"] == "operator":
            return
        await connection.execute(
            """UPDATE tenant_agents SET status=%s,paused_reason=%s,updated_at=now()
               WHERE id=%s AND workspace_id=%s""",
            (
                InstanceStatus.ACTIVE if ready else InstanceStatus.PAUSED,
                None if ready else "not_ready",
                agent_id,
                workspace_id,
            ),
        )

    async def _record_version(
        self,
        connection,
        principal: Principal,
        workspace_id: UUID,
        agent_id: UUID,
        from_version_id: UUID | None,
        to_version_id: UUID,
        action: str,
        request_id: str,
    ) -> None:
        await connection.execute(
            """INSERT INTO tenant_agent_version_events
               (id,workspace_id,tenant_agent_id,from_version_id,to_version_id,action,
                actor_issuer,actor_subject,request_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                uuid4(),
                workspace_id,
                agent_id,
                from_version_id,
                to_version_id,
                action,
                principal.issuer,
                principal.subject,
                request_id,
            ),
        )

    # --------------------------------------------------------------------- bindings
    async def _write_binding(
        self,
        connection,
        workspace_id: UUID,
        agent_id: UUID,
        manifest: AgentManifest,
        binding_key: str,
        integration_id: UUID,
    ) -> None:
        declared = {name for name, _ in manifest.integrations()}
        if binding_key not in declared:
            raise HTTPException(422, "This agent does not use that integration")
        target = await connection.execute(
            """SELECT integration_definition_id,status FROM tenant_integrations
               WHERE id=%s AND workspace_id=%s""",
            (integration_id, workspace_id),
        )
        row = await target.fetchone()
        # A connection from another workspace is simply not found here, and the composite
        # foreign key would refuse the write even if this check were bypassed.
        if row is None:
            raise HTTPException(404, "Unknown connection")
        if row["integration_definition_id"] != binding_key:
            raise HTTPException(422, "That connection is for a different integration")
        if row["status"] == IntegrationStatus.DISABLED:
            raise HTTPException(409, "That connection is disabled")
        await connection.execute(
            """INSERT INTO agent_integration_bindings
               (id,workspace_id,tenant_agent_id,binding_key,tenant_integration_id)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_agent_id,binding_key) DO UPDATE
                   SET tenant_integration_id=EXCLUDED.tenant_integration_id,updated_at=now()""",
            (uuid4(), workspace_id, agent_id, binding_key, integration_id),
        )

    async def set_binding(
        self,
        principal: Principal,
        workspace_id: UUID,
        agent_id: UUID,
        binding_key: str,
        integration_id: UUID,
        request_id: str,
    ) -> TenantAgent:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_AGENTS
            )
            row = await self._row(connection, workspace_id, agent_id, lock=True)
            manifest = await self._manifest(connection, row["version_id"])
            await self._write_binding(
                connection, workspace_id, agent_id, manifest, binding_key, integration_id
            )
            await self._settle_status(connection, workspace_id, agent_id, manifest)
            await self.workspaces.audit(
                connection, principal, workspace_id, "agent.binding_changed", request_id
            )
            return await self._view(
                connection, workspace_id, await self._row(connection, workspace_id, agent_id)
            )

    async def clear_binding(
        self,
        principal: Principal,
        workspace_id: UUID,
        agent_id: UUID,
        binding_key: str,
        request_id: str,
    ) -> TenantAgent:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_AGENTS
            )
            row = await self._row(connection, workspace_id, agent_id, lock=True)
            manifest = await self._manifest(connection, row["version_id"])
            await connection.execute(
                """DELETE FROM agent_integration_bindings
                   WHERE tenant_agent_id=%s AND workspace_id=%s AND binding_key=%s""",
                (agent_id, workspace_id, binding_key),
            )
            await self._settle_status(connection, workspace_id, agent_id, manifest)
            await self.workspaces.audit(
                connection, principal, workspace_id, "agent.binding_changed", request_id
            )
            return await self._view(
                connection, workspace_id, await self._row(connection, workspace_id, agent_id)
            )

    # ------------------------------------------------------------ instance settings
    async def update(
        self,
        principal: Principal,
        workspace_id: UUID,
        agent_id: UUID,
        body: TenantAgentUpdate,
        request_id: str,
    ) -> TenantAgent:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_AGENTS
            )
            row = await self._row(connection, workspace_id, agent_id, lock=True)
            manifest = await self._manifest(connection, row["version_id"])
            bindings = await self._bindings(connection, workspace_id, agent_id)
            requested = body.status
            if requested == InstanceStatus.ACTIVE and not self._readiness(manifest, bindings).ready:
                raise HTTPException(409, "Connect the required integrations before activating")
            changes = body.model_dump(exclude_none=True)
            if "settings" in changes:
                changes["settings"] = Jsonb(changes["settings"])
            if requested is not None:
                # A pause asked for here is recorded as a decision, so no later binding
                # change or version move resumes the agent on its own.
                changes["paused_reason"] = (
                    "operator" if requested == InstanceStatus.PAUSED else None
                )
            assignments = ",".join(f"{field}=%s" for field in changes)
            await connection.execute(
                f"""UPDATE tenant_agents SET {assignments},updated_at=now()
                    WHERE id=%s AND workspace_id=%s""",
                (*changes.values(), agent_id, workspace_id),
            )
            await self.workspaces.audit(
                connection, principal, workspace_id, "agent.settings_changed", request_id
            )
            return await self._view(
                connection, workspace_id, await self._row(connection, workspace_id, agent_id)
            )

    async def uninstall(
        self, principal: Principal, workspace_id: UUID, agent_id: UUID, request_id: str
    ) -> None:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_AGENTS
            )
            await self._row(connection, workspace_id, agent_id, lock=True)
            # Version history is append-only, so an instance is retired rather than erased
            # and its record of what ran when survives.
            await connection.execute(
                """UPDATE tenant_agents SET status='disabled',paused_reason=NULL,updated_at=now()
                   WHERE id=%s AND workspace_id=%s""",
                (agent_id, workspace_id),
            )
            await connection.execute(
                """DELETE FROM agent_integration_bindings
                   WHERE tenant_agent_id=%s AND workspace_id=%s""",
                (agent_id, workspace_id),
            )
            await self.workspaces.audit(
                connection, principal, workspace_id, "agent.disabled", request_id
            )

    # ------------------------------------------------------------- version movement
    async def _version_by_number(self, connection, catalog_agent_id: UUID, version: str):
        result = await connection.execute(
            """SELECT id,version,manifest,status FROM catalog_agent_versions
               WHERE catalog_agent_id=%s AND version=%s""",
            (catalog_agent_id, version),
        )
        row = await result.fetchone()
        if row is None:
            raise HTTPException(404, "Unknown version")
        return row

    async def move_version(
        self,
        principal: Principal,
        workspace_id: UUID,
        agent_id: UUID,
        version: str | None,
        request_id: str,
    ) -> TenantAgent:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_AGENTS
            )
            row = await self._row(connection, workspace_id, agent_id, lock=True)
            if version is None:
                target_id, _ = await offered_version(
                    connection, row["catalog_agent_id"], workspace_id, row["update_channel"]
                )
                if target_id is None:
                    raise HTTPException(409, "No released version is available on this channel")
            else:
                # An explicit version may be older than the offered one: pinning back is a
                # supported move, which is how a tenant stays on 1.4 while 1.5 ships.
                target = await self._version_by_number(connection, row["catalog_agent_id"], version)
                if target["status"] in ("draft", "disabled"):
                    raise HTTPException(409, "That version is not released")
                target_id = target["id"]
            if target_id == row["version_id"]:
                return await self._view(connection, workspace_id, row)

            manifest = await self._manifest(connection, target_id)
            if not self.runtime_version.satisfies_minimum(manifest.runtime_minimum):
                raise HTTPException(
                    409, "That version requires a newer Nexora runtime than this deployment"
                )
            await self._repin(
                connection,
                principal,
                workspace_id,
                agent_id,
                row["version_id"],
                target_id,
                "updated",
                request_id,
            )
            await self._settle_status(connection, workspace_id, agent_id, manifest)
            await self.workspaces.audit(
                connection,
                principal,
                workspace_id,
                "agent.updated",
                request_id,
            )
            return await self._view(
                connection, workspace_id, await self._row(connection, workspace_id, agent_id)
            )

    async def rollback(
        self, principal: Principal, workspace_id: UUID, agent_id: UUID, request_id: str
    ) -> TenantAgent:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_AGENTS
            )
            row = await self._row(connection, workspace_id, agent_id, lock=True)
            previous = await connection.execute(
                """SELECT from_version_id FROM tenant_agent_version_events
                   WHERE tenant_agent_id=%s AND workspace_id=%s AND from_version_id IS NOT NULL
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                (agent_id, workspace_id),
            )
            restore = await previous.fetchone()
            if restore is None or restore["from_version_id"] == row["version_id"]:
                raise HTTPException(409, "There is no earlier version to return to")
            manifest = await self._manifest(connection, restore["from_version_id"])
            await self._repin(
                connection,
                principal,
                workspace_id,
                agent_id,
                row["version_id"],
                restore["from_version_id"],
                "rolled_back",
                request_id,
            )
            await self._settle_status(connection, workspace_id, agent_id, manifest)
            await self.workspaces.audit(
                connection, principal, workspace_id, "agent.rolled_back", request_id
            )
            return await self._view(
                connection, workspace_id, await self._row(connection, workspace_id, agent_id)
            )

    async def _repin(
        self,
        connection,
        principal: Principal,
        workspace_id: UUID,
        agent_id: UUID,
        from_version_id: UUID,
        to_version_id: UUID,
        action: str,
        request_id: str,
    ) -> None:
        """Move the pin only. Settings, overrides and bindings are untouched by design."""
        await connection.execute(
            """UPDATE tenant_agents SET version_id=%s,updated_at=now()
               WHERE id=%s AND workspace_id=%s""",
            (to_version_id, agent_id, workspace_id),
        )
        await self._record_version(
            connection,
            principal,
            workspace_id,
            agent_id,
            from_version_id,
            to_version_id,
            action,
            request_id,
        )

    async def history(
        self,
        principal: Principal,
        workspace_id: UUID,
        agent_id: UUID,
        limit: int,
        cursor: UUID | None,
    ) -> list[VersionEvent]:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            await self._row(connection, workspace_id, agent_id)
            result = await connection.execute(
                """SELECT e.id,e.action,e.actor_subject,e.created_at,
                          f.version AS from_version,t.version AS to_version
                   FROM tenant_agent_version_events e
                   LEFT JOIN catalog_agent_versions f ON f.id=e.from_version_id
                   JOIN catalog_agent_versions t ON t.id=e.to_version_id
                   WHERE e.tenant_agent_id=%s AND e.workspace_id=%s
                     AND (%s::uuid IS NULL OR (e.created_at,e.id) >
                          (SELECT created_at,id FROM tenant_agent_version_events
                           WHERE id=%s::uuid))
                   ORDER BY e.created_at,e.id LIMIT %s""",
                (agent_id, workspace_id, cursor, cursor, limit),
            )
            return [VersionEvent(**row) for row in await result.fetchall()]

    # ------------------------------------------------------------------------ forks
    async def fork(
        self,
        principal: Principal,
        workspace_id: UUID,
        agent_id: UUID,
        body: ForkInput,
        request_id: str,
    ) -> ForkedAgent:
        """Copy an installed standard agent into a workspace-owned custom agent."""
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_AGENTS
            )
            row = await self._row(connection, workspace_id, agent_id)
            manifest = await self._manifest(connection, row["version_id"])
            instructions = (
                body.instructions or row["instructions_override"] or manifest.system_instructions
            )
            custom_id = uuid4()
            result = await connection.execute(
                """INSERT INTO agent_definitions
                   (id,workspace_id,name,instructions,model_profile,created_by_issuer,
                    created_by_subject,origin_catalog_agent_id,origin_version_id,manifest)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id,workspace_id,name,instructions,model_profile,created_at""",
                (
                    custom_id,
                    workspace_id,
                    body.name,
                    instructions,
                    body.model_profile or manifest.model_policy.primary,
                    principal.issuer,
                    principal.subject,
                    row["catalog_agent_id"],
                    row["version_id"],
                    # The manifest is copied, not referenced: a later catalog release
                    # cannot change what this custom agent does.
                    Jsonb(manifest.model_dump(mode="json")),
                ),
            )
            created = await result.fetchone()
            await self.workspaces.audit(
                connection, principal, workspace_id, "agent.forked", request_id
            )
            return ForkedAgent(**created, origin_slug=row["slug"], origin_version=row["version"])

    # -------------------------------------------------------------- update policies
    async def get_policy(self, principal: Principal, workspace_id: UUID) -> UpdatePolicy:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            result = await connection.execute(
                """SELECT workspace_id,channel,mode,updated_at
                   FROM agent_update_policies WHERE workspace_id=%s""",
                (workspace_id,),
            )
            row = await result.fetchone()
            if row is None:
                # The default is the conservative one: stable releases, applied by a person.
                return UpdatePolicy(
                    workspace_id=workspace_id,
                    channel=Channel.STABLE,
                    mode=UpdateMode.MANUAL,
                    updated_at=await self._now(connection),
                )
            return UpdatePolicy(**row)

    async def set_policy(
        self,
        principal: Principal,
        workspace_id: UUID,
        body: UpdatePolicyInput,
        request_id: str,
    ) -> UpdatePolicy:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_AGENTS
            )
            result = await connection.execute(
                """INSERT INTO agent_update_policies (workspace_id,channel,mode)
                   VALUES (%s,%s,%s)
                   ON CONFLICT (workspace_id) DO UPDATE
                       SET channel=EXCLUDED.channel,mode=EXCLUDED.mode,updated_at=now()
                   RETURNING workspace_id,channel,mode,updated_at""",
                (workspace_id, body.channel, body.mode),
            )
            await self.workspaces.audit(
                connection, principal, workspace_id, "agent.update_policy_changed", request_id
            )
            return UpdatePolicy(**await result.fetchone())

    async def apply_automatic_updates(
        self,
        *,
        limit: int = 50,
        cursor: UUID | None = None,
    ) -> tuple[int, UUID | None]:
        """Apply the catalog offer to automatic instances in a bounded, resumable batch.

        This is a system workflow, not a tenant request. Each instance is locked before its
        offer is resolved, version moves remain append-only events, and runtime compatibility
        is checked before the pin changes. A concurrent worker therefore either sees the new
        pin and does nothing or moves it exactly once.
        """
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        actor = Principal(issuer="nexora://system", subject="agent-update-scheduler")
        async with self.connection() as connection:
            result = await connection.execute(
                f"""SELECT {INSTANCE_COLUMNS},a.slug,v.version
                    FROM tenant_agents t
                    JOIN catalog_agents a ON a.id=t.catalog_agent_id
                    JOIN catalog_agent_versions v ON v.id=t.version_id
                    WHERE t.update_mode='automatic' AND t.status<>'disabled'
                      AND (%s::uuid IS NULL OR t.id > %s::uuid)
                    ORDER BY t.id
                    LIMIT %s
                    FOR UPDATE OF t SKIP LOCKED""",
                (cursor, cursor, limit),
            )
            rows = await result.fetchall()
            updated = 0
            for row in rows:
                target_id, target_version = await offered_version(
                    connection,
                    row["catalog_agent_id"],
                    row["workspace_id"],
                    row["update_channel"],
                )
                if target_id is None or target_id == row["version_id"] or target_version is None:
                    continue
                manifest = await self._manifest(connection, target_id)
                if not self.runtime_version.satisfies_minimum(manifest.runtime_minimum):
                    # Fail closed. A later runtime rollout makes this version eligible without
                    # losing the tenant's automatic-update preference.
                    continue
                action = (
                    "rolled_back"
                    if Version(target_version).parts < Version(row["version"]).parts
                    else "updated"
                )
                request_id = f"automatic-agent-update:{row['id']}:{target_id}"
                await self._repin(
                    connection,
                    actor,
                    row["workspace_id"],
                    row["id"],
                    row["version_id"],
                    target_id,
                    action,
                    request_id,
                )
                await self._settle_status(connection, row["workspace_id"], row["id"], manifest)
                await self.workspaces.audit(
                    connection,
                    actor,
                    row["workspace_id"],
                    "agent.automatic_version_changed",
                    request_id,
                )
                updated += 1

            next_cursor = rows[-1]["id"] if len(rows) == limit else None
            return updated, next_cursor

    @staticmethod
    async def _now(connection):
        return (await (await connection.execute("SELECT now() AS at")).fetchone())["at"]
