from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from nexora_api.approvals import ApprovalDecisionInput, ApprovalRecord
from nexora_api.auth import Principal, authenticated
from nexora_api.tool_registry import PolicyDecision, SideEffect


class ToolContractView(BaseModel):
    name: str
    description: str
    schema_version: int
    side_effect: SideEffect
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    effective_policy: PolicyDecision


class ToolPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: PolicyDecision


class ToolPolicyView(BaseModel):
    tool_name: str
    decision: PolicyDecision


class ApprovalPage(BaseModel):
    items: list[ApprovalRecord]


router = APIRouter(prefix="/api/v1", tags=["tools"])
Identity = Annotated[Principal, Depends(authenticated)]


@router.get("/workspaces/{workspace_id}/tools", response_model=list[ToolContractView])
async def list_tools(workspace_id: UUID, principal: Identity, request: Request):
    return await request.app.state.tool_gateway.list_tools(principal, workspace_id)


@router.put(
    "/workspaces/{workspace_id}/tools/{tool_name}/policy",
    response_model=ToolPolicyView,
)
async def set_tool_policy(
    workspace_id: UUID,
    tool_name: str,
    body: ToolPolicyInput,
    principal: Identity,
    request: Request,
):
    return await request.app.state.tool_gateway.set_policy(
        principal,
        workspace_id,
        tool_name,
        body.decision,
        request.state.request_id,
    )


@router.get("/workspaces/{workspace_id}/approvals", response_model=ApprovalPage)
async def list_approvals(
    workspace_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    items = await request.app.state.approvals.list_pending(principal, workspace_id, limit)
    return ApprovalPage(items=items)


@router.post(
    "/workspaces/{workspace_id}/approvals/{approval_id}",
    response_model=ApprovalRecord,
)
async def decide_approval(
    workspace_id: UUID,
    approval_id: UUID,
    body: ApprovalDecisionInput,
    principal: Identity,
    request: Request,
):
    return await request.app.state.approvals.decide(
        principal,
        workspace_id,
        approval_id,
        body,
        request.state.request_id,
    )
