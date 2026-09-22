"""Contracts and the platform Agent Studio API for the standard agent catalog.

A catalog entry is the product ("Social Media Management"); a catalog version is one
immutable published release of it. Versions are append-only: once published, a version's
manifest never changes. Only its release state — status and channel — moves, which is
what lets a tenant stay pinned to 1.4.2 while 1.5.0 rolls out to everyone else.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexora_api.agent_manifest import LABEL, SEMVER, SLUG, AgentManifest
from nexora_api.platform_admin import PlatformIdentity

Slug = Annotated[str, Path(pattern=SLUG, min_length=3, max_length=64)]
SemVer = Annotated[str, Path(pattern=SEMVER.pattern)]


class AgentStatus(StrEnum):
    DRAFT = "draft"
    BETA = "beta"
    STABLE = "stable"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class Channel(StrEnum):
    STABLE = "stable"
    BETA = "beta"
    CANARY = "canary"


class Visibility(StrEnum):
    RESTRICTED = "restricted"
    PUBLIC = "public"


class RolloutState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


# A subscriber to a wider channel also receives everything the narrower channels serve, so
# a canary workspace sees canary, beta and stable releases while a stable one sees only
# stable. Nothing is published to a tenant that did not opt into its channel.
CHANNEL_SCOPE: dict[Channel, tuple[str, ...]] = {
    Channel.STABLE: ("stable",),
    Channel.BETA: ("stable", "beta"),
    Channel.CANARY: ("stable", "beta", "canary"),
}
# Draft and disabled versions are never offered; deprecated ones keep running where they
# are already pinned but are not handed out as an install or update target.
OFFERABLE_STATUSES = ("stable", "beta")


class CatalogAgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: str = Field(pattern=SLUG, min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    category: str = Field(pattern=LABEL)
    icon: str = Field(pattern=LABEL)
    status: AgentStatus = AgentStatus.DRAFT
    visibility: Visibility = Visibility.RESTRICTED


class CatalogAgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    category: str | None = Field(default=None, pattern=LABEL)
    icon: str | None = Field(default=None, pattern=LABEL)
    status: AgentStatus | None = None
    visibility: Visibility | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.model_dump(exclude_none=True):
            raise ValueError("At least one field must be supplied")
        return self


class CatalogAgent(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str
    category: str
    icon: str
    status: AgentStatus
    visibility: Visibility
    latest_version: str | None = None
    version_count: int = 0
    created_at: datetime
    updated_at: datetime


class CatalogAgentPage(BaseModel):
    items: list[CatalogAgent]
    next_cursor: str | None = None


class VersionInput(BaseModel):
    """Publishing takes the whole manifest; the release state comes from the manifest too."""

    model_config = ConfigDict(extra="forbid")

    manifest: AgentManifest


class VersionReleaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AgentStatus | None = None
    channel: Channel | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if self.status is None and self.channel is None:
            raise ValueError("At least one of status or channel must be supplied")
        return self


class CatalogVersion(BaseModel):
    id: UUID
    catalog_agent_id: UUID
    version: str
    status: AgentStatus
    channel: Channel
    min_runtime_version: str
    changelog: str
    manifest: AgentManifest
    created_at: datetime


class CatalogVersionSummary(BaseModel):
    id: UUID
    version: str
    status: AgentStatus
    channel: Channel
    min_runtime_version: str
    changelog: str
    created_at: datetime


class CatalogVersionPage(BaseModel):
    items: list[CatalogVersionSummary]
    next_cursor: str | None = None


class RolloutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=SEMVER.pattern)
    channel: Channel = Channel.STABLE
    percentage: int = Field(ge=0, le=100)


class RolloutUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    percentage: int | None = Field(default=None, ge=0, le=100)
    state: RolloutState | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if self.percentage is None and self.state is None:
            raise ValueError("At least one of percentage or state must be supplied")
        if self.state is RolloutState.ROLLED_BACK:
            raise ValueError("Use the rollback endpoint so the previous version is restored")
        return self


class Rollout(BaseModel):
    id: UUID
    catalog_agent_id: UUID
    version_id: UUID
    version: str
    channel: Channel
    percentage: int
    state: RolloutState
    created_at: datetime
    updated_at: datetime


class RolloutPage(BaseModel):
    items: list[Rollout]
    next_cursor: UUID | None = None


class EntitlementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID


class Entitlement(BaseModel):
    catalog_agent_id: UUID
    workspace_id: UUID
    created_at: datetime


class EntitlementPage(BaseModel):
    items: list[Entitlement]
    next_cursor: UUID | None = None


router = APIRouter(prefix="/api/v1/platform", tags=["agent-studio"])


def repository(request: Request):
    return request.app.state.agent_catalog


@router.post("/agents", response_model=CatalogAgent, status_code=201)
async def create_catalog_agent(
    body: CatalogAgentInput, principal: PlatformIdentity, request: Request
):
    return await repository(request).create_agent(principal, body, request.state.request_id)


@router.get("/agents", response_model=CatalogAgentPage)
async def list_catalog_agents(
    principal: PlatformIdentity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
):
    rows = await repository(request).list_agents(limit + 1, cursor)
    return CatalogAgentPage(
        items=rows[:limit], next_cursor=rows[limit - 1].slug if len(rows) > limit else None
    )


@router.get("/agents/{slug}", response_model=CatalogAgent)
async def get_catalog_agent(slug: Slug, principal: PlatformIdentity, request: Request):
    return await repository(request).get_agent(slug)


@router.patch("/agents/{slug}", response_model=CatalogAgent)
async def update_catalog_agent(
    slug: Slug, body: CatalogAgentUpdate, principal: PlatformIdentity, request: Request
):
    return await repository(request).update_agent(principal, slug, body, request.state.request_id)


@router.post("/agents/{slug}/versions", response_model=CatalogVersion, status_code=201)
async def publish_version(
    slug: Slug, body: VersionInput, principal: PlatformIdentity, request: Request
):
    return await repository(request).publish_version(
        principal, slug, body.manifest, request.state.request_id
    )


@router.get("/agents/{slug}/versions", response_model=CatalogVersionPage)
async def list_versions(
    slug: Slug,
    principal: PlatformIdentity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
):
    rows = await repository(request).list_versions(slug, limit + 1, cursor)
    return CatalogVersionPage(
        items=rows[:limit], next_cursor=rows[limit - 1].version if len(rows) > limit else None
    )


@router.get("/agents/{slug}/versions/{version}", response_model=CatalogVersion)
async def get_version(slug: Slug, version: SemVer, principal: PlatformIdentity, request: Request):
    return await repository(request).get_version(slug, version)


@router.patch("/agents/{slug}/versions/{version}", response_model=CatalogVersion)
async def set_version_release(
    slug: Slug,
    version: SemVer,
    body: VersionReleaseInput,
    principal: PlatformIdentity,
    request: Request,
):
    return await repository(request).set_version_release(
        principal, slug, version, body, request.state.request_id
    )


@router.post("/agents/{slug}/rollouts", response_model=Rollout, status_code=201)
async def create_rollout(
    slug: Slug, body: RolloutInput, principal: PlatformIdentity, request: Request
):
    return await repository(request).create_rollout(principal, slug, body, request.state.request_id)


@router.get("/agents/{slug}/rollouts", response_model=RolloutPage)
async def list_rollouts(
    slug: Slug,
    principal: PlatformIdentity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: UUID | None = None,
):
    rows = await repository(request).list_rollouts(slug, limit + 1, cursor)
    return RolloutPage(
        items=rows[:limit], next_cursor=rows[limit - 1].id if len(rows) > limit else None
    )


@router.patch("/agents/{slug}/rollouts/{rollout_id}", response_model=Rollout)
async def update_rollout(
    slug: Slug,
    rollout_id: UUID,
    body: RolloutUpdate,
    principal: PlatformIdentity,
    request: Request,
):
    return await repository(request).update_rollout(
        principal, slug, rollout_id, body, request.state.request_id
    )


@router.post("/agents/{slug}/rollouts/{rollout_id}/rollback", response_model=Rollout)
async def rollback_rollout(
    slug: Slug, rollout_id: UUID, principal: PlatformIdentity, request: Request
):
    """Stop a rollout and restore the version it replaced for the whole channel."""
    return await repository(request).rollback_rollout(
        principal, slug, rollout_id, request.state.request_id
    )


@router.put("/agents/{slug}/entitlements", response_model=Entitlement, status_code=201)
async def grant_entitlement(
    slug: Slug, body: EntitlementInput, principal: PlatformIdentity, request: Request
):
    return await repository(request).grant_entitlement(
        principal, slug, body.workspace_id, request.state.request_id
    )


@router.delete("/agents/{slug}/entitlements/{workspace_id}", status_code=204)
async def revoke_entitlement(
    slug: Slug, workspace_id: UUID, principal: PlatformIdentity, request: Request
):
    await repository(request).revoke_entitlement(
        principal, slug, workspace_id, request.state.request_id
    )


@router.get("/agents/{slug}/entitlements", response_model=EntitlementPage)
async def list_entitlements(
    slug: Slug,
    principal: PlatformIdentity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: UUID | None = None,
):
    rows = await repository(request).list_entitlements(slug, limit + 1, cursor)
    return EntitlementPage(
        items=rows[:limit], next_cursor=rows[limit - 1].workspace_id if len(rows) > limit else None
    )
