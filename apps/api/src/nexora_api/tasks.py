from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from nexora_api.auth import Principal, authenticated


class RunTaskStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class VerificationState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


class RunTaskEvidence(BaseModel):
    id: UUID
    verification_tool_name: str
    summary: str
    satisfied: bool
    created_at: datetime


class RunTask(BaseModel):
    id: UUID
    workspace_id: UUID
    run_id: UUID
    parent_task_id: UUID | None
    kind: str
    title: str
    description: str
    status: RunTaskStatus
    action_tool_name: str | None
    verification_state: VerificationState
    dependencies: list[UUID]
    evidence: list[RunTaskEvidence]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class RunTaskPage(BaseModel):
    items: list[RunTask]
    next_cursor: UUID | None = None


router = APIRouter(prefix="/api/v1", tags=["tasks"])
Identity = Annotated[Principal, Depends(authenticated)]


def repository(request: Request):
    return request.app.state.run_tasks


@router.get("/workspaces/{workspace_id}/runs/{run_id}/tasks", response_model=RunTaskPage)
async def list_run_tasks(
    workspace_id: UUID,
    run_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: UUID | None = None,
):
    rows = await repository(request).list_tasks(principal, workspace_id, run_id, limit + 1, cursor)
    return RunTaskPage(
        items=rows[:limit],
        next_cursor=rows[limit - 1].id if len(rows) > limit else None,
    )
