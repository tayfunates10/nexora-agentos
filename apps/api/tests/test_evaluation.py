import pytest
from pydantic import ValidationError

from nexora_api.evaluation import (
    EvalCase,
    EvalObservation,
    compare_result,
    evaluate_case,
    regression_rate,
)
from nexora_api.evaluations import AgentRunEvalInput


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


def test_candidate_baseline_comparison_detects_regression_and_improvement():
    baseline_pass = evaluate_case(EvalCase(id="a"), EvalObservation())
    candidate_fail = evaluate_case(
        EvalCase(id="a", forbidden_tools=frozenset({"write"})),
        EvalObservation(selected_tools=frozenset({"write"})),
    )
    baseline_fail = evaluate_case(
        EvalCase(id="b", expected_tools=frozenset({"search"})),
        EvalObservation(),
    )
    candidate_pass = evaluate_case(
        EvalCase(id="b", expected_tools=frozenset({"search"})),
        EvalObservation(selected_tools=frozenset({"search"})),
    )

    regression = compare_result(candidate_fail, baseline_pass)
    improvement = compare_result(candidate_pass, baseline_fail)

    assert regression.regression is True
    assert regression.improvement is False
    assert improvement.regression is False
    assert improvement.improvement is True


def test_comparison_rejects_mismatched_cases():
    candidate = evaluate_case(EvalCase(id="candidate"), EvalObservation())
    baseline = evaluate_case(EvalCase(id="baseline"), EvalObservation())

    with pytest.raises(ValueError, match="case ids"):
        compare_result(candidate, baseline)



def test_agent_run_eval_input_rejects_duplicate_case_or_run_mapping():
    run_id = "11111111-1111-1111-1111-111111111111"
    other_run_id = "22222222-2222-2222-2222-222222222222"

    with pytest.raises(ValidationError, match="case_key"):
        AgentRunEvalInput(
            candidate_label="candidate",
            cases=[
                {"case_key": "case-a", "agent_run_id": run_id},
                {"case_key": "case-a", "agent_run_id": other_run_id},
            ],
        )

    with pytest.raises(ValidationError, match="agent_run_id"):
        AgentRunEvalInput(
            candidate_label="candidate",
            cases=[
                {"case_key": "case-a", "agent_run_id": run_id},
                {"case_key": "case-b", "agent_run_id": run_id},
            ],
        )
