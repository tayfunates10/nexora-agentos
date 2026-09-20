from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel

from nexora_api.auth import Principal, authenticated


class EvalJudgeStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EvalJudgeCaseScore(BaseModel):
    case_id: UUID
    case_key: str
    task_completion: int
    answer_relevance: int
    clarity: int
    quality_milli: int
    rationale: str
    baseline_task_completion: int | None = None
    baseline_answer_relevance: int | None = None
    baseline_clarity: int | None = None
    baseline_quality_milli: int | None = None
    quality_delta_milli: int | None = None
    regression: bool
    improvement: bool
    input_tokens: int
    output_tokens: int
    latency_ms: int


class EvalJudgeRun(BaseModel):
    id: UUID
    workspace_id: UUID
    eval_run_id: UUID
    status: EvalJudgeStatus
    case_count: int
    scored_count: int
    judge_provider: str | None = None
    judge_model: str | None = None
    prompt_version: str | None = None
    error_code: str | None = None
    quality_milli: int | None = None
    baseline_quality_milli: int | None = None
    quality_delta_milli: int | None = None
    regression_count: int
    improvement_count: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    created_at: datetime
    finished_at: datetime | None = None
    results: list[EvalJudgeCaseScore]


router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["evaluation-judges"])
Identity = Annotated[Principal, Depends(authenticated)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


@router.post("/eval-runs/{eval_run_id}/judge-runs", response_model=EvalJudgeRun, status_code=202)
async def create_eval_judge_run(
    workspace_id: UUID,
    eval_run_id: UUID,
    principal: Identity,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKey,
):
    run, created = await request.app.state.eval_judges.create(
        principal,
        workspace_id,
        eval_run_id,
        idempotency_key,
        request.state.request_id,
    )
    response.status_code = 202 if created else 200
    return run


@router.get("/eval-judge-runs/{judge_run_id}", response_model=EvalJudgeRun)
async def get_eval_judge_run(
    workspace_id: UUID,
    judge_run_id: UUID,
    principal: Identity,
    request: Request,
):
    return await request.app.state.eval_judges.get(principal, workspace_id, judge_run_id)
