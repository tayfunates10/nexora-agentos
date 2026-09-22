from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from nexora_api.auth import Principal, authenticated


class ToolSideEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXTERNAL_COMMUNICATION = "external_communication"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ToolCallStatus(StrEnum):
    PLANNED = "planned"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


class RunAction(BaseModel):
    id: UUID
    workspace_id: UUID
    run_id: UUID
    action_name: str
    side_effect: ToolSideEffect
    status: ToolCallStatus
    policy_decision: PolicyDecision
    attempt_count: int
    error_code: str | None = None
    approval_id: UUID | None = None
    approval_status: ApprovalStatus | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunActionPage(BaseModel):
    items: list[RunAction]
    next_cursor: UUID | None = None


class ToolUpsertInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    server_key: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_.-]+$")
    remote_name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict[str, object]
    output_schema: dict[str, object] | None = None
    side_effect: ToolSideEffect
    enabled: bool = True


class ToolDefinition(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    server_key: str
    remote_name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object] | None
    side_effect: ToolSideEffect
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ToolSummary(ToolDefinition):
    # Default deny: a tool with no stored policy is never callable, so the absence of a
    # policy is reported explicitly rather than as an allow.
    policy_decision: PolicyDecision | None = None
    policy_reason: str | None = None
    policy_updated_at: datetime | None = None


class ToolPage(BaseModel):
    items: list[ToolSummary]
    next_cursor: str | None = None


class ToolPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: PolicyDecision
    reason: str = Field(min_length=1, max_length=500)


class ToolPolicy(BaseModel):
    workspace_id: UUID
    tool_id: UUID
    decision: PolicyDecision
    reason: str
    updated_at: datetime


class ApprovalDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern=r"^(approved|rejected)$")


class ToolApproval(BaseModel):
    id: UUID
    workspace_id: UUID
    run_id: UUID
    tool_call_id: UUID
    requested_action: str
    normalized_arguments: dict[str, object]
    requester_subject: str
    approver_subject: str | None = None
    status: ApprovalStatus
    policy_reason: str
    expires_at: datetime
    created_at: datetime
    decided_at: datetime | None = None


class ApprovalPage(BaseModel):
    items: list[ToolApproval]
    next_cursor: UUID | None = None


router = APIRouter(prefix="/api/v1", tags=["tools"])
Identity = Annotated[Principal, Depends(authenticated)]
ToolName = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")]


def repository(request: Request):
    return request.app.state.tool_governance


@router.put("/workspaces/{workspace_id}/tools/{tool_name}", response_model=ToolDefinition)
async def upsert_tool(
    workspace_id: UUID,
    tool_name: ToolName,
    body: ToolUpsertInput,
    principal: Identity,
    request: Request,
):
    return await repository(request).upsert_tool(
        principal, workspace_id, tool_name, body, request.state.request_id
    )


@router.get("/workspaces/{workspace_id}/tools", response_model=ToolPage)
async def list_tools(
    workspace_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
):
    rows = await repository(request).list_tools(principal, workspace_id, limit + 1, cursor)
    return ToolPage(
        items=rows[:limit],
        next_cursor=rows[limit - 1].name if len(rows) > limit else None,
    )


@router.put(
    "/workspaces/{workspace_id}/tools/{tool_name}/policy",
    response_model=ToolPolicy,
)
async def set_tool_policy(
    workspace_id: UUID,
    tool_name: ToolName,
    body: ToolPolicyInput,
    principal: Identity,
    request: Request,
):
    return await repository(request).set_policy(
        principal, workspace_id, tool_name, body, request.state.request_id
    )


@router.get(
    "/workspaces/{workspace_id}/runs/{run_id}/actions",
    response_model=RunActionPage,
)
async def list_run_actions(
    workspace_id: UUID,
    run_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: UUID | None = None,
):
    rows = await repository(request).list_run_actions(
        principal, workspace_id, run_id, limit + 1, cursor
    )
    return RunActionPage(
        items=rows[:limit],
        next_cursor=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.get("/workspaces/{workspace_id}/approvals", response_model=ApprovalPage)
async def list_approvals(
    workspace_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: UUID | None = None,
    status: ApprovalStatus | None = None,
):
    rows = await repository(request).list_approvals(
        principal, workspace_id, limit + 1, cursor, status
    )
    return ApprovalPage(
        items=rows[:limit],
        next_cursor=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.post(
    "/workspaces/{workspace_id}/approvals/{approval_id}/decision",
    response_model=ToolApproval,
)
async def decide_approval(
    workspace_id: UUID,
    approval_id: UUID,
    body: ApprovalDecisionInput,
    principal: Identity,
    request: Request,
):
    return await repository(request).decide_approval(
        principal,
        workspace_id,
        approval_id,
        body.decision,
        request.state.request_id,
    )
