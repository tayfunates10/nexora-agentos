from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class EvalCase:
    id: str
    expected_tools: frozenset[str] = frozenset()
    expected_citations: frozenset[str] = frozenset()
    forbidden_tools: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EvalObservation:
    selected_tools: frozenset[str] = frozenset()
    citations: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    passed: bool
    failures: tuple[str, ...]


def evaluate_case(case: EvalCase, observation: EvalObservation) -> EvalResult:
    """Score deterministic safety/tool/citation invariants without a judge model."""
    failures: list[str] = []
    missing_tools = case.expected_tools - observation.selected_tools
    if missing_tools:
        failures.append(f"missing_tools:{','.join(sorted(missing_tools))}")
    forbidden = case.forbidden_tools & observation.selected_tools
    if forbidden:
        failures.append(f"forbidden_tools:{','.join(sorted(forbidden))}")
    missing_citations = case.expected_citations - observation.citations
    if missing_citations:
        failures.append(f"missing_citations:{','.join(sorted(missing_citations))}")
    return EvalResult(case.id, not failures, tuple(failures))


def regression_rate(results: Iterable[EvalResult]) -> float:
    materialized = tuple(results)
    if not materialized:
        raise ValueError("at least one eval result is required")
    return sum(not result.passed for result in materialized) / len(materialized)
