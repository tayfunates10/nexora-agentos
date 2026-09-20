"""Deterministic retrieval-only evaluation, separate from answer generation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from uuid import UUID

from nexora_api.auth import Principal


@dataclass(frozen=True, slots=True)
class RetrievalEvalCase:
    case_id: str
    query_embedding: tuple[float, ...]
    expected_source_keys: frozenset[str]
    query_text: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.case_id) <= 128:
            raise ValueError("case_id must be between 1 and 128 characters")
        if not 1 <= len(self.query_embedding) <= 4096:
            raise ValueError("query_embedding dimensions must be between 1 and 4096")
        values = tuple(float(value) for value in self.query_embedding)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("query_embedding values must be finite")
        if not any(value != 0 for value in values):
            raise ValueError("query_embedding must not be the zero vector")
        if not self.expected_source_keys:
            raise ValueError("expected_source_keys must not be empty")
        if any(not key or len(key) > 255 for key in self.expected_source_keys):
            raise ValueError("expected source keys must be between 1 and 255 characters")


@dataclass(frozen=True, slots=True)
class RetrievalCaseResult:
    case_id: str
    ranked_source_keys: tuple[str, ...]
    hit: bool
    recall: float
    reciprocal_rank: float


@dataclass(frozen=True, slots=True)
class RetrievalEvalReport:
    k: int
    case_count: int
    hit_rate: float
    mean_recall: float
    mean_reciprocal_rank: float
    cases: tuple[RetrievalCaseResult, ...]


def score_retrieval(
    cases: tuple[RetrievalEvalCase, ...],
    rankings: dict[str, tuple[str, ...]],
    *,
    k: int,
) -> RetrievalEvalReport:
    if not 1 <= k <= 50:
        raise ValueError("k must be between 1 and 50")
    if not cases:
        raise ValueError("at least one retrieval eval case is required")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("retrieval eval case ids must be unique")
    if set(rankings) != set(case_ids):
        raise ValueError("rankings must contain exactly one result for every eval case")

    results: list[RetrievalCaseResult] = []
    for case in cases:
        ranked = tuple(dict.fromkeys(rankings[case.case_id]))[:k]
        relevant_ranks = [
            rank
            for rank, source_key in enumerate(ranked, start=1)
            if source_key in case.expected_source_keys
        ]
        hits = {key for key in ranked if key in case.expected_source_keys}
        results.append(
            RetrievalCaseResult(
                case_id=case.case_id,
                ranked_source_keys=ranked,
                hit=bool(relevant_ranks),
                recall=len(hits) / len(case.expected_source_keys),
                reciprocal_rank=(1.0 / relevant_ranks[0]) if relevant_ranks else 0.0,
            )
        )

    count = len(results)
    return RetrievalEvalReport(
        k=k,
        case_count=count,
        hit_rate=sum(result.hit for result in results) / count,
        mean_recall=sum(result.recall for result in results) / count,
        mean_reciprocal_rank=sum(result.reciprocal_rank for result in results) / count,
        cases=tuple(results),
    )


async def run_retrieval_eval(
    repository,
    principal: Principal,
    workspace_id: UUID,
    *,
    embedding_model: str,
    cases: tuple[RetrievalEvalCase, ...],
    k: int,
    hybrid: bool = False,
    ann_dimensions: int | None = None,
    hnsw_ef_search: int | None = None,
) -> RetrievalEvalReport:
    rankings: dict[str, tuple[str, ...]] = {}
    for case in cases:
        chunks = await repository.retrieve(
            principal,
            workspace_id,
            embedding_model=embedding_model,
            query_embedding=case.query_embedding,
            query_text=case.query_text if hybrid else None,
            limit=k,
            ann_dimensions=ann_dimensions,
            hnsw_ef_search=hnsw_ef_search,
        )
        rankings[case.case_id] = tuple(chunk.source_key for chunk in chunks)
    return score_retrieval(cases, rankings, k=k)
