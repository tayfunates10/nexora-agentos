from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexora_api.auth import Principal, authenticated


BoundedName = Annotated[str, Field(min_length=1, max_length=200)]


class EvalCaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    case_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    input: str = Field(min_length=1, max_length=20000)
    expected_tools: list[BoundedName] = Field(default_factory=list, max_length=32)
    forbidden_tools: list[BoundedName] = Field(default_factory=list, max_length=32)
    expected_citations: list[BoundedName] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_expectations(self):
        for values in (self.expected_tools, self.forbidden_tools, self.expected_citations):
            if len(values) != len(set(values)):
                raise ValueError("evaluation expectations must not contain duplicates")
        if set(self.expected_tools) & set(self.forbidden_tools):
            raise ValueError("a tool cannot be both expected and forbidden")
        return self


class EvalSuiteInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "name": "support-agent",
                    "version": 1,
                    "description": "Deterministic support-agent regression suite.",
                    "cases": [
                        {
                            "case_key": "grounded-read",
                            "input": "Summarize the handbook.",
                            "expected_tools": ["search"],
                            "expected_citations": ["handbook:v1"],
                            "forbidden_tools": ["external_send"],
                        }
                    ],
                }
            ]
        },
    )
    name: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1, le=1000000)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    cases: list[EvalCaseInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_cases(self):
        keys = [case.case_key for case in self.cases]
        if len(keys) != len(set(keys)):
            raise ValueError("case_key values must be unique within a suite")
        return self


class EvalCaseDefinition(BaseModel):
    id: UUID
    case_no: int
    case_key: str
    input: str
    expected_tools: list[str]
    forbidden_tools: list[str]
    expected_citations: list[str]


class EvalSuiteSummary(BaseModel):
    id: UUID
    workspace_id: UUID
    name: str
    version: int
    description: str | None = None
    case_count: int
    created_at: datetime


class EvalSuite(EvalSuiteSummary):
    cases: list[EvalCaseDefinition]


class EvalSuitePage(BaseModel):
    items: list[EvalSuiteSummary]
    next_cursor: UUID | None = None


class EvalObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    selected_tools: list[BoundedName] = Field(default_factory=list, max_length=32)
    citations: list[BoundedName] = Field(default_factory=list, max_length=64)
    raw_output: str | None = Field(default=None, min_length=1, max_length=50000)

    @model_validator(mode="after")
    def validate_sets(self):
        for values in (self.selected_tools, self.citations):
            if any(not value.strip() or value != value.strip() for value in values):
                raise ValueError("tool and citation values must be trimmed and non-empty")
        if len(self.selected_tools) != len(set(self.selected_tools)):
            raise ValueError("selected_tools must not contain duplicates")
        if len(self.citations) != len(set(self.citations)):
            raise ValueError("citations must not contain duplicates")
        return self


class EvalRunInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "candidate_label": "balanced-v2",
                    "baseline_eval_run_id": None,
                    "observations": [
                        {
                            "case_key": "grounded-read",
                            "selected_tools": ["search"],
                            "citations": ["handbook:v1"],
                            "raw_output": "The handbook says ...",
                        }
                    ],
                }
            ]
        },
    )
    candidate_label: str = Field(min_length=1, max_length=128)
    baseline_eval_run_id: UUID | None = None
    observations: list[EvalObservationInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_observations(self):
        keys = [observation.case_key for observation in self.observations]
        if len(keys) != len(set(keys)):
            raise ValueError("each case_key may appear only once")
        return self


class EvalCaseResult(BaseModel):
    case_id: UUID
    case_key: str
    passed: bool
    failures: list[str]
    selected_tools: list[str]
    citations: list[str]
    raw_output: str | None = None
    baseline_passed: bool | None = None
    regression: bool
    improvement: bool


class EvalRun(BaseModel):
    id: UUID
    workspace_id: UUID
    suite_id: UUID
    candidate_label: str
    baseline_eval_run_id: UUID | None = None
    case_count: int
    passed_count: int
    failed_count: int
    regression_count: int
    improvement_count: int
    created_at: datetime
    results: list[EvalCaseResult]


router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["evaluations"])
Identity = Annotated[Principal, Depends(authenticated)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


@router.post("/eval-suites", response_model=EvalSuite, status_code=201)
async def create_eval_suite(
    workspace_id: UUID,
    body: EvalSuiteInput,
    principal: Identity,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKey,
):
    suite, created = await request.app.state.evaluations.create_suite(
        principal,
        workspace_id,
        body,
        idempotency_key,
        request.state.request_id,
    )
    response.status_code = 201 if created else 200
    return suite


@router.get("/eval-suites", response_model=EvalSuitePage)
async def list_eval_suites(
    workspace_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: UUID | None = None,
):
    rows = await request.app.state.evaluations.list_suites(
        principal, workspace_id, limit + 1, cursor
    )
    return EvalSuitePage(
        items=rows[:limit],
        next_cursor=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.get("/eval-suites/{suite_id}", response_model=EvalSuite)
async def get_eval_suite(
    workspace_id: UUID,
    suite_id: UUID,
    principal: Identity,
    request: Request,
):
    return await request.app.state.evaluations.get_suite(principal, workspace_id, suite_id)


@router.post("/eval-suites/{suite_id}/runs", response_model=EvalRun, status_code=201)
async def create_eval_run(
    workspace_id: UUID,
    suite_id: UUID,
    body: EvalRunInput,
    principal: Identity,
    request: Request,
    response: Response,
    idempotency_key: IdempotencyKey,
):
    run, created = await request.app.state.evaluations.create_run(
        principal,
        workspace_id,
        suite_id,
        body,
        idempotency_key,
        request.state.request_id,
    )
    response.status_code = 201 if created else 200
    return run


@router.get("/eval-runs/{eval_run_id}", response_model=EvalRun)
async def get_eval_run(
    workspace_id: UUID,
    eval_run_id: UUID,
    principal: Identity,
    request: Request,
):
    return await request.app.state.evaluations.get_run(principal, workspace_id, eval_run_id)
