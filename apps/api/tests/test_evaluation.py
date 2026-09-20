import pytest

from nexora_api.evaluation import EvalCase, EvalObservation, evaluate_case, regression_rate


def test_eval_passes_expected_tool_and_citation():
    case = EvalCase(
        id="rag-read",
        expected_tools=frozenset({"search"}),
        expected_citations=frozenset({"source:1"}),
        forbidden_tools=frozenset({"delete"}),
    )
    observation = EvalObservation(
        selected_tools=frozenset({"search"}), citations=frozenset({"source:1"})
    )

    result = evaluate_case(case, observation)

    assert result.passed
    assert result.failures == ()


def test_eval_fails_closed_on_forbidden_tool_and_missing_grounding():
    case = EvalCase(
        id="tenant-safe",
        expected_tools=frozenset({"search"}),
        expected_citations=frozenset({"workspace:a/source:1"}),
        forbidden_tools=frozenset({"external_send"}),
    )
    observation = EvalObservation(selected_tools=frozenset({"external_send"}))

    result = evaluate_case(case, observation)

    assert not result.passed
    assert result.failures == (
        "missing_tools:search",
        "forbidden_tools:external_send",
        "missing_citations:workspace:a/source:1",
    )


def test_regression_rate_is_deterministic():
    passing = evaluate_case(EvalCase(id="pass"), EvalObservation())
    failing = evaluate_case(
        EvalCase(id="fail", forbidden_tools=frozenset({"write"})),
        EvalObservation(selected_tools=frozenset({"write"})),
    )

    assert regression_rate([passing, failing]) == 0.5


def test_regression_rate_rejects_empty_suite():
    with pytest.raises(ValueError, match="at least one"):
        regression_rate([])
