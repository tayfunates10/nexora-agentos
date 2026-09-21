"""Contracts and API for a tenant's own agents: installs, bindings, updates and forks.

A standard agent and a tenant's instance of it are different objects. The catalog holds
what Nexora published; a tenant agent holds what this workspace configured — its display
name, its pinned version, its settings, its prompt override and which of its own accounts
each required integration resolves to. Updating the catalog never rewrites an instance,
and forking one produces a custom agent that a later catalog release cannot change.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexora_api.agent_catalog import AgentStatus, Channel
from nexora_api.agent_manifest import SEMVER, SLUG, AgentManifest
from nexora_api.auth import Principal, authenticated
from nexora_api.integrations import IntegrationStatus

BindingKey = Annotated[str, Path(pattern=SLUG, min_length=2, max_length=64)]
MAX_SETTINGS_KEYS = 50


class InstanceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"


class UpdateMode(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"


class IntegrationRequirement(BaseModel):
    """One line of the readiness check a tenant sees before an agent may run."""

    integration_definition_id: str
    name: str
    required: bool
    bound_integration_id: UUID | None = None
    bound_display_name: str | None = None
    bound_status: IntegrationStatus | None = None
    satisfied: bool


class Readiness(BaseModel):
    ready: bool
    requirements: list[IntegrationRequirement]
    missing_required: list[str] = Field(default_factory=list)


class CatalogEntry(BaseModel):
    """A standard agent as offered to this workspace, on its channel."""

    catalog_agent_id: UUID
    slug: str
    name: str
    description: str
    category: str
    icon: str
    status: AgentStatus
    available_version: str | None
    installed_count: int
    required_integrations: list[str]
    optional_integrations: list[str]
    capabilities: list[str]
    approval_required: list[str]


class CatalogEntryPage(BaseModel):
    items: list[CatalogEntry]
    next_cursor: str | None = None


class InstallInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: str = Field(pattern=SLUG, min_length=3, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    update_channel: Channel | None = None
    update_mode: UpdateMode | None = None
    # Which of the workspace's own connections each integration resolves to.
    bindings: dict[str, UUID] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bounded_bindings(self) -> Self:
        if len(self.bindings) > 20:
            raise ValueError("Too many bindings")
        return self


class TenantAgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    status: InstanceStatus | None = None
    update_channel: Channel | None = None
    update_mode: UpdateMode | None = None
    instructions_override: str | None = Field(default=None, max_length=20000)
    settings: dict[str, object] | None = None

    @model_validator(mode="after")
    def _usable(self) -> Self:
        if not self.model_dump(exclude_none=True):
            raise ValueError("At least one field must be supplied")
        if self.settings is not None and len(self.settings) > MAX_SETTINGS_KEYS:
            raise ValueError("Too many settings")
        return self


class VersionChangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Omitted means "whatever this workspace's channel currently offers".
    version: str | None = Field(default=None, pattern=SEMVER.pattern)


class BindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_integration_id: UUID


class AgentBinding(BaseModel):
    binding_key: str
    tenant_integration_id: UUID
    display_name: str
    status: IntegrationStatus
    created_at: datetime
    updated_at: datetime


class TenantAgent(BaseModel):
    id: UUID
    workspace_id: UUID
    catalog_agent_id: UUID
    slug: str
    display_name: str
    version: str
    available_version: str | None
    status: InstanceStatus
    update_channel: Channel
    update_mode: UpdateMode
    instructions_override: str | None
    settings: dict[str, object]
    manifest: AgentManifest
    bindings: list[AgentBinding]
    readiness: Readiness
    created_at: datetime
    updated_at: datetime


class TenantAgentSummary(BaseModel):
    id: UUID
    workspace_id: UUID
    catalog_agent_id: UUID
    slug: str
    display_name: str
    version: str
    available_version: str | None
    status: InstanceStatus
    update_channel: Channel
    update_mode: UpdateMode
    ready: bool
    created_at: datetime
    updated_at: datetime


class TenantAgentPage(BaseModel):
    items: list[TenantAgentSummary]
    next_cursor: UUID | None = None


class VersionEvent(BaseModel):
    id: UUID
    action: str
    from_version: str | None
    to_version: str
    actor_subject: str
    created_at: datetime


class VersionEventPage(BaseModel):
    items: list[VersionEvent]
    next_cursor: UUID | None = None


class ForkInput(BaseModel):
    """Take a private copy. The copy stops tracking the catalog from that moment on."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    instructions: str | None = Field(default=None, min_length=1, max_length=20000)
    model_profile: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    )


class ForkedAgent(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    instructions: str
    model_profile: str
    origin_slug: str
    origin_version: str
    created_at: datetime


class UpdatePolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Channel
    mode: UpdateMode


class UpdatePolicy(BaseModel):
    workspace_id: UUID
    channel: Channel
    mode: UpdateMode
    updated_at: datetime


router = APIRouter(prefix="/api/v1", tags=["tenant-agents"])
Identity = Annotated[Principal, Depends(authenticated)]


def repository(request: Request):
    return request.app.state.tenant_agents


@router.get("/workspaces/{workspace_id}/agent-catalog", response_model=CatalogEntryPage)
async def browse_catalog(
    workspace_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
):
    """Only agents this workspace is entitled to, at the version its channel offers."""
    rows = await repository(request).browse_catalog(principal, workspace_id, limit + 1, cursor)
    return CatalogEntryPage(
        items=rows[:limit], next_cursor=rows[limit - 1].slug if len(rows) > limit else None
    )


@router.post(
    "/workspaces/{workspace_id}/tenant-agents", response_model=TenantAgent, status_code=201
)
async def install_agent(
    workspace_id: UUID, body: InstallInput, principal: Identity, request: Request
):
    return await repository(request).install(
        principal, workspace_id, body, request.state.request_id
    )


@router.get("/workspaces/{workspace_id}/tenant-agents", response_model=TenantAgentPage)
async def list_tenant_agents(
    workspace_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: UUID | None = None,
):
    rows = await repository(request).list_instances(principal, workspace_id, limit + 1, cursor)
    return TenantAgentPage(
        items=rows[:limit], next_cursor=rows[limit - 1].id if len(rows) > limit else None
    )


@router.get("/workspaces/{workspace_id}/tenant-agents/{agent_id}", response_model=TenantAgent)
async def get_tenant_agent(
    workspace_id: UUID, agent_id: UUID, principal: Identity, request: Request
):
    return await repository(request).get(principal, workspace_id, agent_id)


@router.patch("/workspaces/{workspace_id}/tenant-agents/{agent_id}", response_model=TenantAgent)
async def update_tenant_agent(
    workspace_id: UUID,
    agent_id: UUID,
    body: TenantAgentUpdate,
    principal: Identity,
    request: Request,
):
    return await repository(request).update(
        principal, workspace_id, agent_id, body, request.state.request_id
    )


@router.delete("/workspaces/{workspace_id}/tenant-agents/{agent_id}", status_code=204)
async def uninstall_agent(
    workspace_id: UUID, agent_id: UUID, principal: Identity, request: Request
):
    await repository(request).uninstall(principal, workspace_id, agent_id, request.state.request_id)


@router.post(
    "/workspaces/{workspace_id}/tenant-agents/{agent_id}/update", response_model=TenantAgent
)
async def move_version(
    workspace_id: UUID,
    agent_id: UUID,
    body: VersionChangeInput,
    principal: Identity,
    request: Request,
):
    return await repository(request).move_version(
        principal, workspace_id, agent_id, body.version, request.state.request_id
    )


@router.post(
    "/workspaces/{workspace_id}/tenant-agents/{agent_id}/rollback", response_model=TenantAgent
)
async def rollback_version(
    workspace_id: UUID, agent_id: UUID, principal: Identity, request: Request
):
    """Return to the version that ran before the last change, keeping all configuration."""
    return await repository(request).rollback(
        principal, workspace_id, agent_id, request.state.request_id
    )


@router.get(
    "/workspaces/{workspace_id}/tenant-agents/{agent_id}/history", response_model=VersionEventPage
)
async def version_history(
    workspace_id: UUID,
    agent_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: UUID | None = None,
):
    rows = await repository(request).history(principal, workspace_id, agent_id, limit + 1, cursor)
    return VersionEventPage(
        items=rows[:limit], next_cursor=rows[limit - 1].id if len(rows) > limit else None
    )


@router.put(
    "/workspaces/{workspace_id}/tenant-agents/{agent_id}/bindings/{binding_key}",
    response_model=TenantAgent,
)
async def set_binding(
    workspace_id: UUID,
    agent_id: UUID,
    binding_key: BindingKey,
    body: BindingInput,
    principal: Identity,
    request: Request,
):
    return await repository(request).set_binding(
        principal,
        workspace_id,
        agent_id,
        binding_key,
        body.tenant_integration_id,
        request.state.request_id,
    )


@router.delete(
    "/workspaces/{workspace_id}/tenant-agents/{agent_id}/bindings/{binding_key}",
    response_model=TenantAgent,
)
async def clear_binding(
    workspace_id: UUID,
    agent_id: UUID,
    binding_key: BindingKey,
    principal: Identity,
    request: Request,
):
    return await repository(request).clear_binding(
        principal, workspace_id, agent_id, binding_key, request.state.request_id
    )


@router.post(
    "/workspaces/{workspace_id}/tenant-agents/{agent_id}/fork",
    response_model=ForkedAgent,
    status_code=201,
)
async def fork_agent(
    workspace_id: UUID,
    agent_id: UUID,
    body: ForkInput,
    principal: Identity,
    request: Request,
):
    return await repository(request).fork(
        principal, workspace_id, agent_id, body, request.state.request_id
    )


@router.get("/workspaces/{workspace_id}/agent-update-policy", response_model=UpdatePolicy)
async def get_update_policy(workspace_id: UUID, principal: Identity, request: Request):
    return await repository(request).get_policy(principal, workspace_id)


@router.put("/workspaces/{workspace_id}/agent-update-policy", response_model=UpdatePolicy)
async def set_update_policy(
    workspace_id: UUID, body: UpdatePolicyInput, principal: Identity, request: Request
):
    return await repository(request).set_policy(
        principal, workspace_id, body, request.state.request_id
    )
