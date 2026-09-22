"""Contracts and API for the Integration Center and the connector registry.

The single rule this module exists to keep: a stored secret never travels back out. A
credential is written once, and afterwards the platform will tell a tenant that it is
connected, when it last worked, which scopes were granted and what the value's last four
characters are — and nothing else.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexora_api.auth import Principal, authenticated
from nexora_api.connector_mcp import ConnectorToolProvisioner
from nexora_api.integration_manifest import SLUG, ConnectorManifest
from nexora_api.platform_admin import PlatformIdentity

DefinitionId = Annotated[str, Path(pattern=SLUG, min_length=2, max_length=64)]
MAX_CREDENTIAL_FIELDS = 20
MAX_CREDENTIAL_VALUE = 8192


class AuthType(StrEnum):
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    BASIC = "basic"
    BEARER_TOKEN = "bearer_token"
    WEBHOOK = "webhook"


class IntegrationStatus(StrEnum):
    PENDING = "pending"
    CONNECTED = "connected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ERROR = "error"
    DISABLED = "disabled"


class DefinitionStatus(StrEnum):
    AVAILABLE = "available"
    BETA = "beta"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class CredentialFieldView(BaseModel):
    key: str
    label: str
    secret: bool
    required: bool
    help: str = ""
    pattern: str | None = None


class IntegrationDefinitionView(BaseModel):
    """What a tenant may see about a connector. Never includes operator client secrets."""

    id: str
    name: str
    description: str
    category: str
    icon: str
    auth_type: AuthType
    status: DefinitionStatus
    version: str
    capabilities: list[str]
    scopes: list[str]
    credential_fields: list[CredentialFieldView]


class DefinitionPage(BaseModel):
    items: list[IntegrationDefinitionView]
    next_cursor: str | None = None


class CredentialInput(BaseModel):
    """The declared fields a tenant fills in. Validated against the connector definition."""

    model_config = ConfigDict(extra="forbid")

    credentials: dict[str, str] = Field(default_factory=dict)

    @field_validator("credentials")
    @classmethod
    def _bounded(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_CREDENTIAL_FIELDS:
            raise ValueError("Too many credential fields")
        for key, item in value.items():
            if len(key) > 64 or not isinstance(item, str) or len(item) > MAX_CREDENTIAL_VALUE:
                raise ValueError("Credential fields must be short keys with text values")
        return value


class ConnectInput(CredentialInput):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    integration_definition_id: str = Field(pattern=SLUG, min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    # What the tenant recognises the account by: an @handle, a property or account number.
    account_identifier: str = Field(min_length=1, max_length=200)


class IntegrationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    # Only the enable/disable transition is a tenant decision; every other status is an
    # observed fact the platform records for itself.
    enabled: bool | None = None


class TenantIntegration(BaseModel):
    id: UUID
    workspace_id: UUID
    integration_definition_id: str
    display_name: str
    account_identifier: str
    auth_type: AuthType
    status: IntegrationStatus
    scopes: list[str]
    granted_scopes: list[str]
    config: dict[str, str]
    # The masked tail of the stored secret. The value itself never leaves the vault.
    credential_hint: str | None = None
    credential_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_tested_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None


class IntegrationPage(BaseModel):
    items: list[TenantIntegration]
    next_cursor: UUID | None = None


class ConnectionTest(BaseModel):
    integration_id: UUID
    status: IntegrationStatus
    ok: bool
    checked_at: datetime
    granted_scopes: list[str] = Field(default_factory=list)
    missing_scopes: list[str] = Field(default_factory=list)
    error_code: str | None = None


class OAuthStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    integration_definition_id: str = Field(pattern=SLUG, min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    account_identifier: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(default_factory=list, max_length=50)


class OAuthStart(BaseModel):
    integration_id: UUID
    authorization_url: str
    state: str


class OAuthCompleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    state: str = Field(min_length=16, max_length=128)
    code: str = Field(min_length=1, max_length=2048)


router = APIRouter(prefix="/api/v1", tags=["integrations"])
platform_router = APIRouter(prefix="/api/v1/platform", tags=["integration-registry"])
Identity = Annotated[Principal, Depends(authenticated)]


def repository(request: Request):
    return request.app.state.integrations


@platform_router.put("/integrations/{definition_id}", response_model=IntegrationDefinitionView)
async def publish_definition(
    definition_id: DefinitionId,
    body: ConnectorManifest,
    principal: PlatformIdentity,
    request: Request,
):
    """Publish or update a connector definition. New services need no core code change."""
    return await repository(request).publish_definition(
        principal, definition_id, body, request.state.request_id
    )


@router.get("/integration-catalog", response_model=DefinitionPage)
async def list_definitions(
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
):
    rows = await repository(request).list_definitions(limit + 1, cursor)
    return DefinitionPage(
        items=rows[:limit], next_cursor=rows[limit - 1].id if len(rows) > limit else None
    )


@router.get("/workspaces/{workspace_id}/integrations", response_model=IntegrationPage)
async def list_integrations(
    workspace_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: UUID | None = None,
):
    rows = await repository(request).list_integrations(principal, workspace_id, limit + 1, cursor)
    return IntegrationPage(
        items=rows[:limit], next_cursor=rows[limit - 1].id if len(rows) > limit else None
    )


@router.post(
    "/workspaces/{workspace_id}/integrations", response_model=TenantIntegration, status_code=201
)
async def connect_integration(
    workspace_id: UUID, body: ConnectInput, principal: Identity, request: Request
):
    integration = await repository(request).connect(
        principal, workspace_id, body, request.state.request_id
    )
    await ConnectorToolProvisioner(request.app.state.settings).sync(
        principal, integration, request.state.request_id
    )
    return integration


@router.get(
    "/workspaces/{workspace_id}/integrations/{integration_id}", response_model=TenantIntegration
)
async def get_integration(
    workspace_id: UUID, integration_id: UUID, principal: Identity, request: Request
):
    return await repository(request).get(principal, workspace_id, integration_id)


@router.patch(
    "/workspaces/{workspace_id}/integrations/{integration_id}", response_model=TenantIntegration
)
async def update_integration(
    workspace_id: UUID,
    integration_id: UUID,
    body: IntegrationUpdate,
    principal: Identity,
    request: Request,
):
    return await repository(request).update(
        principal, workspace_id, integration_id, body, request.state.request_id
    )


@router.put(
    "/workspaces/{workspace_id}/integrations/{integration_id}/credential",
    response_model=TenantIntegration,
)
async def rotate_credential(
    workspace_id: UUID,
    integration_id: UUID,
    body: CredentialInput,
    principal: Identity,
    request: Request,
):
    """Replace the stored secret. The previous ciphertext is destroyed, not archived."""
    return await repository(request).rotate(
        principal, workspace_id, integration_id, body, request.state.request_id
    )


@router.delete("/workspaces/{workspace_id}/integrations/{integration_id}", status_code=204)
async def disconnect_integration(
    workspace_id: UUID, integration_id: UUID, principal: Identity, request: Request
):
    await repository(request).disconnect(
        principal, workspace_id, integration_id, request.state.request_id
    )
    await ConnectorToolProvisioner(request.app.state.settings).disable(
        workspace_id, integration_id
    )


@router.post(
    "/workspaces/{workspace_id}/integrations/{integration_id}/test", response_model=ConnectionTest
)
async def test_integration(
    workspace_id: UUID,
    integration_id: UUID,
    principal: Identity,
    request: Request,
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    return await repository(request).test(
        principal, workspace_id, integration_id, request.state.request_id
    )


@router.post(
    "/workspaces/{workspace_id}/integrations/oauth/start",
    response_model=OAuthStart,
    status_code=201,
)
async def start_oauth(
    workspace_id: UUID,
    body: OAuthStartInput,
    principal: Identity,
    request: Request,
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    return await repository(request).start_oauth(
        principal, workspace_id, body, request.state.request_id
    )


@router.post(
    "/workspaces/{workspace_id}/integrations/oauth/complete", response_model=TenantIntegration
)
async def complete_oauth(
    workspace_id: UUID, body: OAuthCompleteInput, principal: Identity, request: Request
):
    integration = await repository(request).complete_oauth(
        principal, workspace_id, body, request.state.request_id
    )
    await ConnectorToolProvisioner(request.app.state.settings).sync(
        principal, integration, request.state.request_id
    )
    return integration
