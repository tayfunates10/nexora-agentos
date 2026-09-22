"""The Integration Vault: tenant connections and the credentials behind them.

Every path through this module upholds three properties.

*Persistence.* A connection survives the session that created it. Credentials live in the
vault keyed to the workspace, so signing out, closing the browser and returning tomorrow
shows the connection still there — unless the provider actually revoked it, which is
recorded as an expired connection with a reconnect path rather than as silence.

*Confidentiality.* Plaintext exists only inside a single function call. It is written to
the vault, and from then on the only representation anyone outside can obtain is a masked
hint. No response model, log line, audit record or agent prompt carries more.

*Isolation.* Every read is scoped to a workspace the caller belongs to, and every stored
ciphertext is bound to its workspace by associated data, so a row read out of the wrong
tenant's context fails authentication rather than decrypting.
"""

import base64
import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.connector_runtime import ConnectorError, ConnectorRuntime
from nexora_api.integration_manifest import (
    ConnectorManifest,
    load_connector,
    secure_https_url,
)
from nexora_api.integrations import (
    ConnectInput,
    ConnectionTest,
    CredentialFieldView,
    CredentialInput,
    IntegrationDefinitionView,
    IntegrationStatus,
    IntegrationUpdate,
    OAuthCompleteInput,
    OAuthStart,
    OAuthStartInput,
    TenantIntegration,
)
from nexora_api.secret_vault import (
    SealedSecret,
    SecretUnreadable,
    VaultKeyUnknown,
    VaultUnavailable,
    credential_aad,
    mask,
)
from nexora_api.tool_contracts import validate_registration_schema
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission

OAUTH_STATE_TTL_SECONDS = 600
CREDENTIAL_PURPOSE = "credential"
OAUTH_PURPOSE = "oauth_verifier"
INTEGRATION_COLUMNS = """id,workspace_id,integration_definition_id,display_name,account_identifier,
                         auth_type,credential_reference,status,scopes,granted_scopes,config,
                         created_at,updated_at,last_tested_at,last_success_at,last_error"""


class OAuthClient(BaseModel):
    """An operator-registered OAuth application. Secrets stay in the process environment."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_id: str = Field(min_length=1, max_length=255)
    client_secret: str = Field(min_length=1, max_length=512)
    redirect_uri: str = Field(min_length=1, max_length=500)


def oauth_clients(settings: Settings) -> dict[str, OAuthClient]:
    document = settings.integration_oauth_clients
    if document is None or not document.get_secret_value():
        return {}
    try:
        parsed = json.loads(document.get_secret_value())
        return {key: OAuthClient.model_validate(value) for key, value in parsed.items()}
    except (json.JSONDecodeError, ValidationError, AttributeError):
        raise RuntimeError("integration_oauth_clients is not a valid client registry") from None


class IntegrationRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.workspaces = WorkspaceRepository(settings)
        self.vault = settings.build_secret_vault()
        self.runtime = ConnectorRuntime(settings.integration_request_timeout_seconds)
        self._clients = oauth_clients(settings)
        # Definitions change rarely and are read on every connect, test and agent call, so
        # the parsed manifest is kept per process and refreshed when its version moves.
        self._definitions: dict[str, ConnectorManifest] = {}

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ) as connection:
            yield connection

    # ---------------------------------------------------------------- connector registry
    @staticmethod
    def _definition_view(row) -> IntegrationDefinitionView:
        return IntegrationDefinitionView(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            category=row["category"],
            icon=row["icon"],
            auth_type=row["auth_type"],
            status=row["status"],
            version=row["version"],
            capabilities=row["capabilities"],
            scopes=row["scopes"],
            credential_fields=[CredentialFieldView(**field) for field in row["credential_fields"]],
        )

    @staticmethod
    def _managed_tool_schema(method: str) -> dict:
        properties: dict[str, object] = {
            "query": {"type": "object", "additionalProperties": True},
        }
        if method == "GET":
            return {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            }
        properties["idempotency_key"] = {
            "type": "string",
            "minLength": 8,
            "maxLength": 128,
        }
        required = ["idempotency_key"]
        if method == "POST":
            properties["payload"] = {"type": "object", "additionalProperties": True}
            required.append("payload")
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    @staticmethod
    def _managed_tool_side_effect(method: str, capability: str) -> str:
        if method == "GET":
            return "read"
        if method == "DELETE":
            return "destructive"
        if any(word in capability for word in ("publish", "reply", "send", "deliver", "message")):
            return "external_communication"
        return "write"

    async def _reconcile_existing_connector_tools(
        self,
        connection,
        principal: Principal,
        manifest: ConnectorManifest,
    ) -> None:
        """Expose newly executable endpoints to workspaces already using this connector."""
        workspaces = await connection.execute(
            """SELECT DISTINCT workspace_id
               FROM tenant_integrations
               WHERE integration_definition_id=%s AND status<>'disabled'""",
            (manifest.id,),
        )
        workspace_ids = [row["workspace_id"] for row in await workspaces.fetchall()]
        for endpoint in manifest.endpoints:
            side_effect = self._managed_tool_side_effect(endpoint.method, endpoint.capability)
            schema = self._managed_tool_schema(endpoint.method)
            validate_registration_schema(schema, side_effect=side_effect)
            tool_name = f"{manifest.id}.{endpoint.capability}"
            for workspace_id in workspace_ids:
                tool_result = await connection.execute(
                    """INSERT INTO tool_definitions
                       (id,workspace_id,name,server_key,remote_name,description,input_schema,
                        output_schema,side_effect,enabled,created_by_issuer,created_by_subject)
                       VALUES (%s,%s,%s,'nexora-integrations',%s,%s,%s,NULL,%s,true,%s,%s)
                       ON CONFLICT (workspace_id,name) DO UPDATE SET
                         server_key='nexora-integrations',
                         remote_name=EXCLUDED.remote_name,
                         description=EXCLUDED.description,
                         input_schema=EXCLUDED.input_schema,
                         output_schema=NULL,
                         side_effect=EXCLUDED.side_effect,
                         enabled=true,
                         updated_at=now()
                       RETURNING id""",
                    (
                        uuid4(),
                        workspace_id,
                        tool_name,
                        tool_name,
                        f"Managed {manifest.name} capability: {endpoint.capability}",
                        Jsonb(schema),
                        side_effect,
                        principal.issuer,
                        principal.subject,
                    ),
                )
                tool_id = (await tool_result.fetchone())["id"]
                decision = "allow" if side_effect == "read" else "require_approval"
                await connection.execute(
                    """INSERT INTO tool_policies
                       (workspace_id,tool_id,decision,reason,
                        updated_by_issuer,updated_by_subject)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (workspace_id,tool_id) DO NOTHING""",
                    (
                        workspace_id,
                        tool_id,
                        decision,
                        "Managed connector default policy",
                        principal.issuer,
                        principal.subject,
                    ),
                )

    async def publish_definition(
        self,
        principal: Principal,
        definition_id: str,
        manifest: ConnectorManifest,
        request_id: str,
    ) -> IntegrationDefinitionView:
        if manifest.id != definition_id:
            raise HTTPException(422, "Connector id does not match the published definition")
        async with self.connection() as connection:
            result = await connection.execute(
                """INSERT INTO integration_definitions
                   (id,name,description,category,icon,auth_type,capabilities,scopes,
                    credential_fields,status,version,manifest)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO UPDATE SET
                       name=EXCLUDED.name,description=EXCLUDED.description,
                       category=EXCLUDED.category,icon=EXCLUDED.icon,
                       auth_type=EXCLUDED.auth_type,capabilities=EXCLUDED.capabilities,
                       scopes=EXCLUDED.scopes,credential_fields=EXCLUDED.credential_fields,
                       status=EXCLUDED.status,version=EXCLUDED.version,
                       manifest=EXCLUDED.manifest,updated_at=now()
                   RETURNING id,name,description,category,icon,auth_type,capabilities,scopes,
                             credential_fields,status,version""",
                (
                    manifest.id,
                    manifest.name,
                    manifest.description,
                    manifest.category,
                    manifest.icon,
                    manifest.auth,
                    Jsonb(manifest.capabilities),
                    Jsonb(manifest.scopes),
                    Jsonb([field.model_dump() for field in manifest.credential_fields]),
                    manifest.status,
                    manifest.version,
                    Jsonb(manifest.model_dump(mode="json")),
                ),
            )
            row = await result.fetchone()
            # The whole definition is stored so the runtime can build requests from it;
            # only the tenant-visible projection is returned.
            await connection.execute(
                """INSERT INTO platform_events
                   (id,actor_issuer,actor_subject,action,request_id,target,detail)
                   VALUES (%s,%s,%s,'registry.connector.published',%s,%s,%s)""",
                (
                    uuid4(),
                    principal.issuer,
                    principal.subject,
                    request_id,
                    manifest.id,
                    Jsonb({"version": manifest.version, "status": manifest.status}),
                ),
            )
            await self._reconcile_existing_connector_tools(connection, principal, manifest)
            self._definitions[manifest.id] = manifest
            return self._definition_view(row)

    async def _definition(self, connection, definition_id: str) -> ConnectorManifest:
        """The published connector definition, parsed once per process and per version."""
        result = await connection.execute(
            "SELECT version,manifest FROM integration_definitions WHERE id=%s",
            (definition_id,),
        )
        row = await result.fetchone()
        if row is None:
            raise HTTPException(404, "Unknown integration")
        cached = self._definitions.get(definition_id)
        if cached is not None and cached.version == row["version"]:
            return cached
        manifest = load_connector(row["manifest"])
        self._definitions[definition_id] = manifest
        return manifest

    async def list_definitions(
        self, limit: int, cursor: str | None
    ) -> list[IntegrationDefinitionView]:
        async with self.connection() as connection:
            result = await connection.execute(
                """SELECT id,name,description,category,icon,auth_type,capabilities,scopes,
                          credential_fields,status,version
                   FROM integration_definitions
                   WHERE status<>'disabled' AND (%s::text IS NULL OR id > %s::text)
                   ORDER BY id LIMIT %s""",
                (cursor, cursor, limit),
            )
            return [self._definition_view(row) for row in await result.fetchall()]

    # ------------------------------------------------------------- tenant integrations
    @staticmethod
    def _integration(row, hint: str | None, expires_at) -> TenantIntegration:
        return TenantIntegration(
            id=row["id"],
            workspace_id=row["workspace_id"],
            integration_definition_id=row["integration_definition_id"],
            display_name=row["display_name"],
            account_identifier=row["account_identifier"],
            auth_type=row["auth_type"],
            status=row["status"],
            scopes=row["scopes"],
            granted_scopes=row["granted_scopes"],
            config=row["config"],
            credential_hint=hint,
            credential_expires_at=expires_at,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_tested_at=row["last_tested_at"],
            last_success_at=row["last_success_at"],
            last_error=row["last_error"],
        )

    async def list_integrations(
        self, principal: Principal, workspace_id: UUID, limit: int, cursor: UUID | None
    ) -> list[TenantIntegration]:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            result = await connection.execute(
                f"""SELECT {INTEGRATION_COLUMNS},
                       (SELECT c.hint FROM integration_credentials c
                        WHERE c.id=t.credential_reference) AS hint,
                       (SELECT c.expires_at FROM integration_credentials c
                        WHERE c.id=t.credential_reference) AS credential_expires_at
                    FROM tenant_integrations t
                    WHERE workspace_id=%s AND (%s::uuid IS NULL OR id > %s::uuid)
                    ORDER BY id LIMIT %s""",
                (workspace_id, cursor, cursor, limit),
            )
            return [
                self._integration(row, row["hint"], row["credential_expires_at"])
                for row in await result.fetchall()
            ]

    async def _row(self, connection, workspace_id: UUID, integration_id: UUID, lock: bool = False):
        result = await connection.execute(
            f"""SELECT {INTEGRATION_COLUMNS} FROM tenant_integrations
                WHERE workspace_id=%s AND id=%s"""
            + (" FOR UPDATE" if lock else ""),
            (workspace_id, integration_id),
        )
        row = await result.fetchone()
        if row is None:
            raise HTTPException(404)
        return row

    async def _credential_meta(self, connection, reference: UUID | None):
        if reference is None:
            return None, None
        result = await connection.execute(
            "SELECT hint,expires_at FROM integration_credentials WHERE id=%s", (reference,)
        )
        row = await result.fetchone()
        return (row["hint"], row["expires_at"]) if row else (None, None)

    async def _view(self, connection, row) -> TenantIntegration:
        hint, expires_at = await self._credential_meta(connection, row["credential_reference"])
        return self._integration(row, hint, expires_at)

    async def get(
        self, principal: Principal, workspace_id: UUID, integration_id: UUID
    ) -> TenantIntegration:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            return await self._view(
                connection, await self._row(connection, workspace_id, integration_id)
            )

    def _split_fields(
        self, definition: ConnectorManifest, values: dict[str, str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Separate what must be sealed from what may be shown."""
        try:
            definition.validate_credential(values)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        secret_keys = definition.secret_field_keys
        sealed = {key: value for key, value in values.items() if key in secret_keys and value}
        config = {key: value for key, value in values.items() if key not in secret_keys and value}
        if definition.base_url_field and definition.base_url_field in config:
            try:
                config[definition.base_url_field] = secure_https_url(
                    config[definition.base_url_field].rstrip("/"), "base URL"
                )
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
        return sealed, config

    def _primary_secret(self, definition: ConnectorManifest, sealed: dict[str, str]) -> str:
        placement = definition.credential_placement
        preferred = placement.value_field if placement else None
        if preferred and preferred in sealed:
            return sealed[preferred]
        return next(iter(sealed.values()), "")

    def _seal(
        self,
        workspace_id: UUID,
        integration_id: UUID,
        definition: ConnectorManifest,
        document: dict[str, str],
    ) -> SealedSecret:
        aad = credential_aad(workspace_id, integration_id, CREDENTIAL_PURPOSE)
        try:
            sealed = self.vault.seal(json.dumps(document, separators=(",", ":")), aad)
        except VaultUnavailable:
            raise HTTPException(503, "Secret storage is not configured") from None
        # The hint describes the credential a person recognises, not the JSON envelope.
        return replace(sealed, hint=mask(self._primary_secret(definition, document)))

    def _unseal(self, workspace_id: UUID, integration_id: UUID, row) -> dict[str, str]:
        aad = credential_aad(workspace_id, integration_id, CREDENTIAL_PURPOSE)
        sealed = SealedSecret(
            key_id=row["key_id"],
            wrapped_key=bytes(row["wrapped_key"]),
            wrap_nonce=bytes(row["wrap_nonce"]),
            nonce=bytes(row["nonce"]),
            ciphertext=bytes(row["ciphertext"]),
            hint=row["hint"],
        )
        try:
            return json.loads(self.vault.open(sealed, aad))
        except VaultUnavailable:
            raise HTTPException(503, "Secret storage is not configured") from None
        except (VaultKeyUnknown, SecretUnreadable, json.JSONDecodeError):
            raise HTTPException(409, "Stored credential could not be read") from None

    async def _write_credential(
        self,
        connection,
        workspace_id: UUID,
        integration_id: UUID,
        definition: ConnectorManifest,
        document: dict[str, str],
        expires_at: datetime | None = None,
    ) -> UUID:
        sealed = self._seal(workspace_id, integration_id, definition, document)
        credential_id = uuid4()
        await connection.execute(
            """INSERT INTO integration_credentials
               (id,workspace_id,tenant_integration_id,key_id,wrapped_key,wrap_nonce,nonce,
                ciphertext,hint,expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                credential_id,
                workspace_id,
                integration_id,
                sealed.key_id,
                sealed.wrapped_key,
                sealed.wrap_nonce,
                sealed.nonce,
                sealed.ciphertext,
                sealed.hint,
                expires_at,
            ),
        )
        superseded = await connection.execute(
            "SELECT credential_reference FROM tenant_integrations WHERE id=%s", (integration_id,)
        )
        previous = (await superseded.fetchone())["credential_reference"]
        await connection.execute(
            """UPDATE tenant_integrations
               SET credential_reference=%s,status='connected',last_error=NULL,updated_at=now()
               WHERE id=%s AND workspace_id=%s""",
            (credential_id, integration_id, workspace_id),
        )
        if previous is not None:
            # A rotated secret is destroyed, not archived: there is no second copy to leak.
            await connection.execute(
                "DELETE FROM integration_credentials WHERE id=%s AND workspace_id=%s",
                (previous, workspace_id),
            )
        return credential_id

    async def connect(
        self, principal: Principal, workspace_id: UUID, body: ConnectInput, request_id: str
    ) -> TenantIntegration:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_INTEGRATIONS
            )
            definition = await self._definition(connection, body.integration_definition_id)
            if definition.status == "disabled":
                raise HTTPException(409, "This integration is not available")
            if definition.auth == "oauth2":
                raise HTTPException(
                    409, "This integration is connected through its authorization flow"
                )
            sealed, config = self._split_fields(definition, body.credentials)
            if not sealed:
                raise HTTPException(422, "This integration requires a secret")

            integration_id = uuid4()
            try:
                await connection.execute(
                    """INSERT INTO tenant_integrations
                       (id,workspace_id,integration_definition_id,display_name,account_identifier,
                        auth_type,status,scopes,granted_scopes,config,
                        created_by_issuer,created_by_subject)
                       VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,'[]'::jsonb,%s,%s,%s)""",
                    (
                        integration_id,
                        workspace_id,
                        definition.id,
                        body.display_name,
                        body.account_identifier,
                        definition.auth,
                        Jsonb(definition.scopes),
                        Jsonb(config),
                        principal.issuer,
                        principal.subject,
                    ),
                )
            except psycopg.errors.UniqueViolation:
                raise HTTPException(
                    409, "This account is already connected for this integration"
                ) from None
            await self._write_credential(
                connection, workspace_id, integration_id, definition, sealed
            )
            await self.workspaces.audit(
                connection, principal, workspace_id, "integration.added", request_id
            )
            return await self._view(
                connection, await self._row(connection, workspace_id, integration_id)
            )

    async def update(
        self,
        principal: Principal,
        workspace_id: UUID,
        integration_id: UUID,
        body: IntegrationUpdate,
        request_id: str,
    ) -> TenantIntegration:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_INTEGRATIONS
            )
            row = await self._row(connection, workspace_id, integration_id, lock=True)
            display_name = body.display_name or row["display_name"]
            status = row["status"]
            if body.enabled is not None:
                if body.enabled and row["status"] == IntegrationStatus.DISABLED:
                    # Re-enabling asserts nothing about the credential; the next test does.
                    status = (
                        IntegrationStatus.CONNECTED
                        if row["credential_reference"]
                        else IntegrationStatus.PENDING
                    )
                elif not body.enabled:
                    status = IntegrationStatus.DISABLED
            await connection.execute(
                """UPDATE tenant_integrations SET display_name=%s,status=%s,updated_at=now()
                   WHERE id=%s AND workspace_id=%s""",
                (display_name, status, integration_id, workspace_id),
            )
            if body.enabled is not None:
                await self.workspaces.audit(
                    connection,
                    principal,
                    workspace_id,
                    "integration.enabled" if body.enabled else "integration.disabled",
                    request_id,
                )
            return await self._view(
                connection, await self._row(connection, workspace_id, integration_id)
            )

    async def rotate(
        self,
        principal: Principal,
        workspace_id: UUID,
        integration_id: UUID,
        body: CredentialInput,
        request_id: str,
    ) -> TenantIntegration:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_INTEGRATIONS
            )
            row = await self._row(connection, workspace_id, integration_id, lock=True)
            definition = await self._definition(connection, row["integration_definition_id"])
            if definition.auth == "oauth2":
                raise HTTPException(409, "Reconnect this integration through its provider")
            sealed, config = self._split_fields(definition, body.credentials)
            if not sealed:
                raise HTTPException(422, "This integration requires a secret")
            await connection.execute(
                "UPDATE tenant_integrations SET config=%s WHERE id=%s AND workspace_id=%s",
                (Jsonb(config), integration_id, workspace_id),
            )
            await self._write_credential(
                connection, workspace_id, integration_id, definition, sealed
            )
            await self.workspaces.audit(
                connection, principal, workspace_id, "integration.credential_rotated", request_id
            )
            return await self._view(
                connection, await self._row(connection, workspace_id, integration_id)
            )

    async def disconnect(
        self, principal: Principal, workspace_id: UUID, integration_id: UUID, request_id: str
    ) -> None:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_INTEGRATIONS
            )
            await self._row(connection, workspace_id, integration_id, lock=True)
            bound = await connection.execute(
                """SELECT count(*) AS bindings FROM agent_integration_bindings
                   WHERE tenant_integration_id=%s AND workspace_id=%s""",
                (integration_id, workspace_id),
            )
            if (await bound.fetchone())["bindings"]:
                raise HTTPException(
                    409, "Remove this connection from its agents before disconnecting it"
                )
            # The reference is cleared first so the ciphertext can be destroyed; the row
            # itself follows.
            await connection.execute(
                """UPDATE tenant_integrations SET credential_reference=NULL,updated_at=now()
                   WHERE id=%s AND workspace_id=%s""",
                (integration_id, workspace_id),
            )
            await connection.execute(
                """DELETE FROM integration_credentials
                   WHERE tenant_integration_id=%s AND workspace_id=%s""",
                (integration_id, workspace_id),
            )
            await connection.execute(
                """DELETE FROM integration_oauth_states
                   WHERE tenant_integration_id=%s AND workspace_id=%s""",
                (integration_id, workspace_id),
            )
            await connection.execute(
                "DELETE FROM tenant_integrations WHERE id=%s AND workspace_id=%s",
                (integration_id, workspace_id),
            )
            await self.workspaces.audit(
                connection, principal, workspace_id, "integration.removed", request_id
            )

    # ------------------------------------------------------------------ resolution
    async def resolve(
        self, connection, workspace_id: UUID, integration_id: UUID
    ) -> tuple[ConnectorManifest, dict[str, str], dict[str, str], dict]:
        """Open a usable credential for the connector runtime. Callers keep it in-process."""
        row = await self._row(connection, workspace_id, integration_id)
        if row["status"] in (IntegrationStatus.DISABLED, IntegrationStatus.REVOKED):
            raise HTTPException(409, "This connection is not usable")
        if row["credential_reference"] is None:
            raise HTTPException(409, "This connection has no credential")
        definition = await self._definition(connection, row["integration_definition_id"])
        stored = await connection.execute(
            """SELECT key_id,wrapped_key,wrap_nonce,nonce,ciphertext,hint,expires_at
               FROM integration_credentials WHERE id=%s AND workspace_id=%s""",
            (row["credential_reference"], workspace_id),
        )
        credential_row = await stored.fetchone()
        if credential_row is None:
            raise HTTPException(409, "This connection has no credential")
        document = self._unseal(workspace_id, integration_id, credential_row)
        return definition, document, row["config"], row

    async def invoke_bound(
        self,
        workspace_id: UUID,
        agent_id: UUID,
        tool_name: str,
        arguments: dict[str, object],
    ):
        """Execute a connector capability through the installed agent's own binding."""
        binding_key, separator, capability = tool_name.partition(".")
        if not separator or not capability:
            raise ConnectorError("invalid_integration_tool")
        async with self.connection() as connection:
            bound = await connection.execute(
                """SELECT b.tenant_integration_id,t.status AS agent_status,
                          i.status AS integration_status
                   FROM agent_integration_bindings b
                   JOIN tenant_agents t
                     ON t.id=b.tenant_agent_id AND t.workspace_id=b.workspace_id
                   JOIN tenant_integrations i
                     ON i.id=b.tenant_integration_id AND i.workspace_id=b.workspace_id
                   WHERE b.workspace_id=%s AND b.tenant_agent_id=%s AND b.binding_key=%s""",
                (workspace_id, agent_id, binding_key),
            )
            binding = await bound.fetchone()
            if binding is None:
                raise ConnectorError("integration_not_bound")
            if binding["agent_status"] != "active":
                raise ConnectorError("agent_not_active")
            if binding["integration_status"] != IntegrationStatus.CONNECTED:
                raise ConnectorError("integration_not_connected")

            integration_id = binding["tenant_integration_id"]
            definition, document, config, _ = await self.resolve(
                connection, workspace_id, integration_id
            )
            if definition.id != binding_key or capability not in definition.capabilities:
                raise ConnectorError("capability_not_declared")
            document = await self._refresh_if_needed(
                connection, workspace_id, integration_id, definition, document
            )
            return await self.runtime.invoke(
                definition,
                capability,
                document,
                config,
                arguments,
            )

    async def test(
        self, principal: Principal, workspace_id: UUID, integration_id: UUID, request_id: str
    ) -> ConnectionTest:
        """Prove the stored credential still works, and record what the provider answered."""
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            definition, document, config, row = await self.resolve(
                connection, workspace_id, integration_id
            )
            document = await self._refresh_if_needed(
                connection, workspace_id, integration_id, definition, document
            )
            checked_at = datetime.now(UTC)
            granted = list(row["granted_scopes"]) or list(row["scopes"])
            missing = sorted(set(row["scopes"]) - set(granted))

            if definition.test_capability is None:
                # Nothing cheap and read-only to call: report the stored state honestly
                # rather than implying a round trip that never happened.
                await self._record_test(connection, workspace_id, integration_id, None)
                return ConnectionTest(
                    integration_id=integration_id,
                    status=row["status"],
                    ok=row["status"] == IntegrationStatus.CONNECTED,
                    checked_at=checked_at,
                    granted_scopes=granted,
                    missing_scopes=missing,
                    error_code="no_probe_available",
                )
            try:
                result = await self.runtime.invoke(
                    definition, definition.test_capability, document, config
                )
            except ConnectorError as error:
                await self._record_test(connection, workspace_id, integration_id, error.code)
                return ConnectionTest(
                    integration_id=integration_id,
                    status=IntegrationStatus.ERROR,
                    ok=False,
                    checked_at=checked_at,
                    granted_scopes=granted,
                    missing_scopes=missing,
                    error_code=error.code,
                )

            status = (
                IntegrationStatus.CONNECTED
                if result.ok
                else IntegrationStatus.EXPIRED
                if result.credential_rejected
                else IntegrationStatus.ERROR
            )
            await self._record_test(
                connection, workspace_id, integration_id, result.error_code, status
            )
            return ConnectionTest(
                integration_id=integration_id,
                status=status,
                ok=result.ok,
                checked_at=checked_at,
                granted_scopes=granted,
                missing_scopes=missing,
                error_code=result.error_code,
            )

    async def _record_test(
        self,
        connection,
        workspace_id: UUID,
        integration_id: UUID,
        error_code: str | None,
        status: str | None = None,
    ) -> None:
        # Only the stable error code is stored. Provider bodies can echo request detail,
        # so they never reach a column that a console renders.
        await connection.execute(
            """UPDATE tenant_integrations
               SET last_tested_at=now(),
                   last_success_at=CASE WHEN %s IS NULL THEN now() ELSE last_success_at END,
                   last_error=%s,
                   status=COALESCE(%s,status),
                   updated_at=now()
               WHERE id=%s AND workspace_id=%s""",
            (error_code, error_code, status, integration_id, workspace_id),
        )

    # ---------------------------------------------------------------------- OAuth
    def _client(self, definition_id: str) -> OAuthClient:
        client = self._clients.get(definition_id)
        if client is None:
            raise HTTPException(503, "This integration is not registered for this deployment")
        return client

    async def start_oauth(
        self, principal: Principal, workspace_id: UUID, body: OAuthStartInput, request_id: str
    ) -> OAuthStart:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_INTEGRATIONS
            )
            definition = await self._definition(connection, body.integration_definition_id)
            if definition.auth != "oauth2" or definition.oauth is None:
                raise HTTPException(409, "This integration does not use an authorization flow")
            if definition.status == "disabled":
                raise HTTPException(409, "This integration is not available")
            client = self._client(definition.id)
            requested = body.scopes or definition.oauth.default_scopes
            unknown = sorted(set(requested) - set(definition.scopes))
            if unknown:
                raise HTTPException(422, f"Undeclared scopes requested: {unknown}")

            integration_id = uuid4()
            try:
                await connection.execute(
                    """INSERT INTO tenant_integrations
                       (id,workspace_id,integration_definition_id,display_name,account_identifier,
                        auth_type,status,scopes,granted_scopes,config,
                        created_by_issuer,created_by_subject)
                       VALUES (%s,%s,%s,%s,%s,'oauth2','pending',%s,
                               '[]'::jsonb,'{}'::jsonb,%s,%s)""",
                    (
                        integration_id,
                        workspace_id,
                        definition.id,
                        body.display_name,
                        body.account_identifier,
                        Jsonb(requested),
                        principal.issuer,
                        principal.subject,
                    ),
                )
            except psycopg.errors.UniqueViolation:
                raise HTTPException(
                    409, "This account is already connected for this integration"
                ) from None

            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            aad = credential_aad(workspace_id, integration_id, OAUTH_PURPOSE)
            try:
                sealed = self.vault.seal(verifier, aad)
            except VaultUnavailable:
                raise HTTPException(503, "Secret storage is not configured") from None
            await connection.execute(
                """INSERT INTO integration_oauth_states
                   (state_digest,workspace_id,integration_definition_id,tenant_integration_id,
                    key_id,wrapped_key,wrap_nonce,nonce,ciphertext,redirect_uri,requested_scopes,
                    expires_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    hashlib.sha256(state.encode()).hexdigest(),
                    workspace_id,
                    definition.id,
                    integration_id,
                    sealed.key_id,
                    sealed.wrapped_key,
                    sealed.wrap_nonce,
                    sealed.nonce,
                    sealed.ciphertext,
                    client.redirect_uri,
                    Jsonb(requested),
                    datetime.now(UTC) + timedelta(seconds=OAUTH_STATE_TTL_SECONDS),
                ),
            )
            await self.workspaces.audit(
                connection, principal, workspace_id, "integration.oauth_started", request_id
            )
            query = urlencode(
                {
                    "client_id": client.client_id,
                    "redirect_uri": client.redirect_uri,
                    "response_type": "code",
                    "scope": " ".join(requested),
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
            return OAuthStart(
                integration_id=integration_id,
                authorization_url=f"{definition.oauth.authorize_url}?{query}",
                state=state,
            )

    async def complete_oauth(
        self, principal: Principal, workspace_id: UUID, body: OAuthCompleteInput, request_id: str
    ) -> TenantIntegration:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_INTEGRATIONS
            )
            digest = hashlib.sha256(body.state.encode()).hexdigest()
            stored = await connection.execute(
                """SELECT * FROM integration_oauth_states
                   WHERE state_digest=%s AND workspace_id=%s FOR UPDATE""",
                (digest, workspace_id),
            )
            state_row = await stored.fetchone()
            # A state is single use: it is consumed whether or not the exchange succeeds.
            await connection.execute(
                "DELETE FROM integration_oauth_states WHERE state_digest=%s", (digest,)
            )
            if state_row is None or state_row["expires_at"] <= datetime.now(UTC):
                raise HTTPException(409, "This authorization request is no longer valid")

            integration_id = state_row["tenant_integration_id"]
            definition = await self._definition(connection, state_row["integration_definition_id"])
            verifier = self.vault.open(
                SealedSecret(
                    key_id=state_row["key_id"],
                    wrapped_key=bytes(state_row["wrapped_key"]),
                    wrap_nonce=bytes(state_row["wrap_nonce"]),
                    nonce=bytes(state_row["nonce"]),
                    ciphertext=bytes(state_row["ciphertext"]),
                    hint="",
                ),
                credential_aad(workspace_id, integration_id, OAUTH_PURPOSE),
            )
            client = self._client(definition.id)
            tokens = await self._exchange(
                definition,
                {
                    "grant_type": "authorization_code",
                    "code": body.code,
                    "redirect_uri": state_row["redirect_uri"],
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                    "code_verifier": verifier,
                },
            )
            granted = tokens.pop("granted_scopes", list(state_row["requested_scopes"]))
            await connection.execute(
                "UPDATE tenant_integrations SET granted_scopes=%s WHERE id=%s AND workspace_id=%s",
                (Jsonb(granted), integration_id, workspace_id),
            )
            await self._write_credential(
                connection,
                workspace_id,
                integration_id,
                definition,
                {key: value for key, value in tokens.items() if key != "expires_at"},
                tokens.get("expires_at"),
            )
            await self.workspaces.audit(
                connection, principal, workspace_id, "integration.added", request_id
            )
            return await self._view(
                connection, await self._row(connection, workspace_id, integration_id)
            )

    async def _exchange(self, definition: ConnectorManifest, form: dict[str, str]) -> dict:
        """Trade a code or refresh token for a new access token at the provider."""
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.integration_request_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    definition.oauth.token_url,
                    data=form,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError:
            raise HTTPException(502, "The provider could not be reached") from None
        if not response.is_success:
            raise HTTPException(502, "The provider rejected the authorization")
        try:
            payload = response.json()
        except ValueError:
            raise HTTPException(502, "The provider returned an unreadable response") from None

        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise HTTPException(502, "The provider returned no access token")
        document: dict = {"access_token": access}
        refresh = payload.get("refresh_token")
        if isinstance(refresh, str) and refresh:
            document["refresh_token"] = refresh
        lifetime = payload.get("expires_in")
        if isinstance(lifetime, int) and 0 < lifetime <= 60 * 60 * 24 * 400:
            document["expires_at"] = datetime.now(UTC) + timedelta(seconds=lifetime)
        scope = payload.get("scope")
        if isinstance(scope, str) and scope:
            document["granted_scopes"] = scope.split()
        return document

    async def _refresh_if_needed(
        self,
        connection,
        workspace_id: UUID,
        integration_id: UUID,
        definition: ConnectorManifest,
        document: dict[str, str],
    ) -> dict[str, str]:
        """Renew an access token before it expires, with nobody present to be asked."""
        if definition.auth != "oauth2" or definition.oauth is None:
            return document
        if not definition.oauth.refreshable or "refresh_token" not in document:
            return document
        stored = await connection.execute(
            """SELECT expires_at FROM integration_credentials
               WHERE tenant_integration_id=%s AND workspace_id=%s
               ORDER BY created_at DESC LIMIT 1""",
            (integration_id, workspace_id),
        )
        row = await stored.fetchone()
        expires_at = row["expires_at"] if row else None
        margin = timedelta(seconds=definition.oauth.refresh_margin_seconds)
        if expires_at is None or expires_at - margin > datetime.now(UTC):
            return document

        client = self._client(definition.id)
        refreshed = await self._exchange(
            definition,
            {
                "grant_type": "refresh_token",
                "refresh_token": document["refresh_token"],
                "client_id": client.client_id,
                "client_secret": client.client_secret,
            },
        )
        # Providers that do not reissue a refresh token keep the existing one working.
        refreshed.setdefault("refresh_token", document["refresh_token"])
        refreshed.pop("granted_scopes", None)
        expiry = refreshed.pop("expires_at", None)
        await self._write_credential(
            connection, workspace_id, integration_id, definition, refreshed, expiry
        )
        return refreshed
