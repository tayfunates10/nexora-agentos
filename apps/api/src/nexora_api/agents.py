from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from nexora_api.auth import Principal, authenticated


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=20000)
    model_profile: str = Field(
        default="default", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    )


class AgentDefinition(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    instructions: str
    model_profile: str
    created_at: datetime


class AgentPage(BaseModel):
    items: list[AgentDefinition]
    next_cursor: UUID | None = None


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    agent_id: UUID
    input: str = Field(min_length=1, max_length=20000)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRun(BaseModel):
    id: UUID
    workspace_id: UUID
    agent_id: UUID
    trace_id: UUID
    status: RunStatus
    attempt_count: int
    cancel_requested_at: datetime | None = None
    finished_at: datetime | None = None
    failure_code: str | None = None
    created_at: datetime
    updated_at: datetime


class RunEvent(BaseModel):
    id: UUID
    event_no: int
    event_type: str
    payload: dict[str, object]
    created_at: datetime


class RunEventPage(BaseModel):
    items: list[RunEvent]
    next_cursor: int | None = None


router = APIRouter(prefix="/api/v1", tags=["agents"])
Identity = Annotated[Principal, Depends(authenticated)]


def repository(request: Request):
    return request.app.state.agent_runtime


@router.post("/workspaces/{workspace_id}/agents", response_model=AgentDefinition, status_code=201)
async def create_agent(workspace_id: UUID, body: AgentInput, principal: Identity, request: Request):
    return await repository(request).create_agent(
        principal, workspace_id, body, request.state.request_id
    )


@router.get("/workspaces/{workspace_id}/agents", response_model=AgentPage)
async def list_agents(
    workspace_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: UUID | None = None,
):
    rows = await repository(request).list_agents(principal, workspace_id, limit + 1, cursor)
    return AgentPage(
        items=rows[:limit], next_cursor=rows[limit - 1].id if len(rows) > limit else None
    )


@router.get("/workspaces/{workspace_id}/agents/{agent_id}", response_model=AgentDefinition)
async def get_agent(workspace_id: UUID, agent_id: UUID, principal: Identity, request: Request):
    return await repository(request).get_agent(principal, workspace_id, agent_id)


@router.post("/workspaces/{workspace_id}/runs", response_model=AgentRun, status_code=201)
async def create_run(
    workspace_id: UUID,
    body: RunInput,
    principal: Identity,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
):
    run, created = await repository(request).create_run(
        principal, workspace_id, body, idempotency_key, request.state.request_id
    )
    response.status_code = 201 if created else 200
    return run


@router.get("/workspaces/{workspace_id}/runs/{run_id}", response_model=AgentRun)
async def get_run(workspace_id: UUID, run_id: UUID, principal: Identity, request: Request):
    return await repository(request).get_run(principal, workspace_id, run_id)


@router.post("/workspaces/{workspace_id}/runs/{run_id}/cancel", response_model=AgentRun)
async def cancel_run(workspace_id: UUID, run_id: UUID, principal: Identity, request: Request):
    return await repository(request).cancel_run(
        principal, workspace_id, run_id, request.state.request_id
    )


@router.get(
    "/workspaces/{workspace_id}/runs/{run_id}/events",
    response_model=RunEventPage,
)
async def list_run_events(
    workspace_id: UUID,
    run_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[int, Query(ge=0)] = 0,
):
    rows = await repository(request).list_events(principal, workspace_id, run_id, limit + 1, cursor)
    return RunEventPage(
        items=rows[:limit],
        next_cursor=rows[limit - 1].event_no if len(rows) > limit else None,
    )
