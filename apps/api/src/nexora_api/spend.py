"""Workspace spend accounting and budget enforcement.

Cost is recorded in micros: one micro is a millionth of one unit of the operator
accounting currency. A deployment declares a single currency through its pricing data;
currency conversion is never inferred from stored records.

Prices are operator configuration. A run, an agent or a tenant cannot influence them.
Cost is computed once, when the provider call is recorded, so a later price change
cannot rewrite what an already executed run cost.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexora_api.auth import Principal, authenticated

TOKENS_PER_PRICE_UNIT = 1_000_000
MAX_PRICE_MICROS = 1_000_000_000_000
MAX_LIMIT_MICROS = 1_000_000_000_000_000
MAX_SOURCE_KEY_LENGTH = 200
MAX_ALERT_THRESHOLDS = 5
DEFAULT_ALERT_THRESHOLDS = (80, 100)


class SpendCategory(StrEnum):
    AGENT_RUN = "agent_run"
    EVALUATION_JUDGE = "evaluation_judge"
    EMBEDDING = "embedding"


class BudgetEnforcement(StrEnum):
    ENFORCE = "enforce"
    MONITOR = "monitor"


class SpendLimitExceeded(RuntimeError):
    """A workspace budget refuses further paid work. Raised before provider egress."""

    def __init__(self, code: str = "workspace_budget_exhausted"):
        super().__init__(code)
        self.code = code


class SpendPricingError(RuntimeError):
    """A priced call whose model has no operator price. Accounting fails closed."""

    def __init__(self, code: str = "model_price_not_configured"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_micros_per_million_tokens: int
    output_micros_per_million_tokens: int

    def __post_init__(self):
        for rate in (self.input_micros_per_million_tokens, self.output_micros_per_million_tokens):
            if not isinstance(rate, int) or isinstance(rate, bool):
                raise ValueError("model prices must be integers")
            if not 0 <= rate <= MAX_PRICE_MICROS:
                raise ValueError("model prices must be between 0 and 10^12 micros")

    def cost_micros(self, input_tokens: int, output_tokens: int) -> int:
        """Integer cost of one call. A partial price unit rounds up, never down."""
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts cannot be negative")
        total = (
            input_tokens * self.input_micros_per_million_tokens
            + output_tokens * self.output_micros_per_million_tokens
        )
        return -(-total // TOKENS_PER_PRICE_UNIT)


@dataclass(frozen=True, slots=True)
class SpendPolicy:
    """Operator pricing for every model a worker may call."""

    prices: Mapping[tuple[str, str], ModelPrice]

    def price(self, provider: str, model: str) -> ModelPrice:
        price = self.prices.get((provider, model))
        if price is None:
            # An unpriced call would silently escape accounting and every budget.
            raise SpendPricingError()
        return price

    def cost_micros(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> int:
        return self.price(provider, model).cost_micros(input_tokens, output_tokens)


@dataclass(frozen=True, slots=True)
class EmbeddingSpend:
    """Operator pricing for the single embedding model a deployment may call.

    Embeddings report input tokens only; an output rate would invent a charge.
    """

    provider: str
    model: str
    price: ModelPrice

    def cost_micros(self, input_tokens: int) -> int:
        return self.price.cost_micros(input_tokens, 0)


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    reason: str
    limit_micros: int | None
    consumed_micros: int

    @property
    def remaining_micros(self) -> int | None:
        if self.limit_micros is None:
            return None
        return max(0, self.limit_micros - self.consumed_micros)


def budget_decision(
    limit_micros: int | None,
    enforcement: BudgetEnforcement | None,
    consumed_micros: int,
) -> BudgetDecision:
    """Decide whether a workspace may start another priced call this period.

    The check runs before provider egress, so the call that crosses the limit is the
    last one allowed. Overshoot is bounded by the execution profile token limits.
    """
    if consumed_micros < 0:
        raise ValueError("consumed spend cannot be negative")
    if limit_micros is None or enforcement is None:
        return BudgetDecision(True, "no_limit", None, consumed_micros)
    if consumed_micros < limit_micros:
        return BudgetDecision(True, "within_budget", limit_micros, consumed_micros)
    if enforcement is BudgetEnforcement.MONITOR:
        return BudgetDecision(True, "monitor", limit_micros, consumed_micros)
    return BudgetDecision(False, "exhausted", limit_micros, consumed_micros)


def agent_step_source_key(run_id: UUID, step_no: int) -> str:
    return f"agent-run:{run_id}:step:{step_no}"


def judge_case_source_key(judge_run_id: UUID, case_id: UUID) -> str:
    return f"eval-judge:{judge_run_id}:case:{case_id}"


def run_retrieval_source_key(run_id: UUID) -> str:
    return f"agent-run:{run_id}:retrieval"


def answer_cache_embedding_source_key(run_id: UUID) -> str:
    return f"agent-run:{run_id}:answer-cache-embedding"


def adhoc_retrieval_source_key(reference: UUID) -> str:
    """A retrieval that belongs to no durable run still has to be charged somewhere."""
    return f"retrieval:{reference}"


def knowledge_source_key(source_id: UUID) -> str:
    return f"knowledge-source:{source_id}"


def normalize_thresholds(values: Sequence[int]) -> tuple[int, ...]:
    """Duplicate or unordered thresholds would alert twice on one crossing."""
    thresholds = tuple(sorted(set(values)))
    if len(thresholds) > MAX_ALERT_THRESHOLDS:
        raise ValueError(f"at most {MAX_ALERT_THRESHOLDS} alert thresholds are allowed")
    if any(not 1 <= threshold <= 100 for threshold in thresholds):
        raise ValueError("alert thresholds must be between 1 and 100 percent")
    return thresholds


def crossed_thresholds(
    thresholds: Sequence[int],
    limit_micros: int | None,
    consumed_micros: int,
) -> tuple[int, ...]:
    """Thresholds a period has reached, compared without floating point."""
    if limit_micros is None:
        return ()
    return tuple(
        threshold
        for threshold in sorted(set(thresholds))
        if consumed_micros * 100 >= threshold * limit_micros
    )


class BudgetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthly_limit_micros: int = Field(ge=0, le=MAX_LIMIT_MICROS)
    enforcement: BudgetEnforcement = BudgetEnforcement.ENFORCE
    alert_thresholds: list[int] = Field(
        default_factory=lambda: list(DEFAULT_ALERT_THRESHOLDS),
        max_length=MAX_ALERT_THRESHOLDS,
    )

    @field_validator("alert_thresholds")
    @classmethod
    def _thresholds(cls, value: list[int]) -> list[int]:
        return list(normalize_thresholds(value))


class Budget(BaseModel):
    workspace_id: UUID
    monthly_limit_micros: int
    enforcement: BudgetEnforcement
    alert_thresholds: list[int]
    updated_at: datetime


class SpendAlert(BaseModel):
    threshold_percent: int
    monthly_limit_micros: int
    consumed_micros: int
    enforcement: BudgetEnforcement
    created_at: datetime


class SpendCategoryTotal(BaseModel):
    category: SpendCategory
    call_count: int
    input_tokens: int
    output_tokens: int
    cost_micros: int


class SpendSummary(BaseModel):
    workspace_id: UUID
    period_start: datetime
    period_end: datetime
    consumed_micros: int
    monthly_limit_micros: int | None = None
    enforcement: BudgetEnforcement | None = None
    remaining_micros: int | None = None
    exhausted: bool = False
    alert_thresholds: list[int] = Field(default_factory=list)
    alerts: list[SpendAlert] = Field(default_factory=list)
    categories: list[SpendCategoryTotal] = Field(default_factory=list)


class SpendRecord(BaseModel):
    id: UUID
    source_key: str
    category: SpendCategory
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_micros: int
    occurred_at: datetime


class SpendRecordPage(BaseModel):
    items: list[SpendRecord]
    next_cursor: UUID | None = None


router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["spend"])
Identity = Annotated[Principal, Depends(authenticated)]


def repository(request: Request):
    return request.app.state.spend


@router.get("/spend", response_model=SpendSummary)
async def read_spend(workspace_id: UUID, principal: Identity, request: Request):
    return await repository(request).summary(principal, workspace_id)


@router.put("/spend/budget", response_model=Budget)
async def set_budget(
    workspace_id: UUID,
    body: BudgetInput,
    principal: Identity,
    request: Request,
):
    return await repository(request).set_budget(
        principal, workspace_id, body, request.state.request_id
    )


@router.get("/spend/records", response_model=SpendRecordPage)
async def list_spend_records(
    workspace_id: UUID,
    principal: Identity,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: UUID | None = None,
    category: SpendCategory | None = None,
):
    rows = await repository(request).list_records(
        principal, workspace_id, limit + 1, cursor, category
    )
    if len(rows) > limit:
        return SpendRecordPage(items=rows[:limit], next_cursor=rows[limit - 1].id)
    return SpendRecordPage(items=rows)
