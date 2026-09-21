from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from nexora_api.auth import Principal, authenticated


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class Permission(StrEnum):
    READ = "workspace:read"
    UPDATE = "workspace:update"
    MANAGE_MEMBERS = "workspace:members:manage"
    MANAGE_AGENTS = "agent:manage"
    RUN_AGENTS = "agent:run"
    CANCEL_ANY_RUN = "agent:run:cancel:any"
    MANAGE_TOOLS = "tool:manage"
    APPROVE_TOOLS = "tool:approve"
    MANAGE_KNOWLEDGE = "knowledge:manage"
    MANAGE_EVALS = "eval:manage"
    MANAGE_SPEND = "spend:manage"
    MANAGE_INTEGRATIONS = "integration:manage"


GRANTS = {
    Role.OWNER: frozenset(Permission),
    Role.ADMIN: frozenset(
        {
            Permission.READ,
            Permission.UPDATE,
            Permission.MANAGE_AGENTS,
            Permission.RUN_AGENTS,
            Permission.CANCEL_ANY_RUN,
            Permission.MANAGE_TOOLS,
            Permission.APPROVE_TOOLS,
            Permission.MANAGE_KNOWLEDGE,
            Permission.MANAGE_EVALS,
            Permission.MANAGE_SPEND,
            Permission.MANAGE_INTEGRATIONS,
        }
    ),
    Role.MEMBER: frozenset({Permission.READ, Permission.RUN_AGENTS}),
}


def authorize(role: str | None, permission: Permission):
    if role is None:
        raise HTTPException(404)
    if permission not in GRANTS.get(role, frozenset()):
        raise HTTPException(403)


class WorkspaceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)


class Workspace(BaseModel):
    id: UUID
    name: str
    role: Role


class WorkspacePage(BaseModel):
    items: list[Workspace]
    next_cursor: UUID | None = None


class MemberInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    subject: str = Field(min_length=1, max_length=255)
    role: Role


class Membership(BaseModel):
    subject: str
    role: Role


router = APIRouter(prefix="/api/v1", tags=["workspaces"])
Identity = Annotated[Principal, Depends(authenticated)]


def repository(request: Request):
    return request.app.state.workspaces


@router.get("/me")
async def me(principal: Identity):
    return {"issuer": principal.issuer, "subject": principal.subject}


@router.post("/workspaces", response_model=Workspace, status_code=201)
async def create_workspace(body: WorkspaceInput, principal: Identity, request: Request):
    return await repository(request).create(principal, body.name, request.state.request_id)


@router.get("/workspaces", response_model=WorkspacePage)
async def list_workspaces(
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: UUID | None = None,
):
    rows = await repository(request).list(principal, limit + 1, cursor)
    return WorkspacePage(
        items=rows[:limit], next_cursor=rows[limit - 1].id if len(rows) > limit else None
    )


@router.get("/workspaces/{workspace_id}", response_model=Workspace)
async def get_workspace(workspace_id: UUID, principal: Identity, request: Request):
    return await repository(request).get(principal, workspace_id)


@router.patch("/workspaces/{workspace_id}", response_model=Workspace)
async def rename_workspace(
    workspace_id: UUID,
    body: WorkspaceInput,
    principal: Identity,
    request: Request,
):
    return await repository(request).rename(
        principal, workspace_id, body.name, request.state.request_id
    )


@router.put("/workspaces/{workspace_id}/members", response_model=Membership)
async def set_member(
    workspace_id: UUID,
    body: MemberInput,
    principal: Identity,
    request: Request,
):
    return await repository(request).set_member(
        principal, workspace_id, body.subject, body.role, request.state.request_id
    )
