from copy import deepcopy
from uuid import uuid4

import pytest

from nexora_api.run_results import summarize_result


def run(status="succeeded"):
    return {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "agent_id": uuid4(),
        "trace_id": uuid4(),
        "status": status,
        "failure_code": None if status == "succeeded" else "execution_failed",
    }


def step(number=0, text="  Final answer\n", reason="stop", calls=None):
    return {
        "step_no": number,
        "provider": "test",
        "model": "model-v1",
        "response": {
            "text": text,
            "finish_reason": reason,
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "tool_calls": calls or [],
            "structured_output": {"private": "excluded"},
        },
    }


def test_result_aggregates_recorded_steps_without_exposing_arguments_or_intermediate_text():
    first = step(
        text="private intermediate",
        reason="tool_call",
        calls=[
            {"name": "lookup", "arguments": {"secret": "do-not-return"}},
            {"name": "lookup", "arguments": {"other": "do-not-return"}},
        ],
    )
    result = summarize_result(run(), [first, step(1)])
    assert result.output_text == "  Final answer\n"
    assert result.recorded_input_tokens == 20
    assert result.recorded_output_tokens == 4
    assert result.selected_tools == ["lookup"]
    assert [item.step_no for item in result.model_steps] == [0, 1]
    assert result.finish_reason == "stop"
    serialized = result.model_dump_json()
    for secret in ("private intermediate", "do-not-return", "structured_output", "arguments"):
        assert secret not in serialized


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_unsuccessful_runs_never_expose_partial_answers(status):
    result = summarize_result(run(status), [step(text="incomplete sensitive answer")])
    assert result.output_text is None
    assert result.finish_reason is None
    assert result.recorded_input_tokens == 10
    assert "incomplete sensitive answer" not in result.model_dump_json()
    assert summarize_result(run(status), []).model_steps == []


def test_refusal_remains_explicit_in_final_result():
    result = summarize_result(run(), [step(text="Cannot comply.", reason="refusal")])
    assert result.finish_reason == "refusal"
    assert result.output_text == "Cannot comply."


@pytest.mark.parametrize("status", ["queued", "running", "waiting_for_approval"])
def test_nonterminal_runs_cannot_publish_results(status):
    with pytest.raises(ValueError, match="not_ready"):
        summarize_result(run(status), [step()])


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [step(1)],
        [step(), step(2)],
        [step(), step()],
        [step(text="")],
        [step(text=None)],
        [step(reason="length")],
        [step(reason="tool_call", calls=[{"name": "delete"}])],
        [step(calls=[{"name": "delete"}])],
        [step(i) for i in range(33)],
    ],
)
def test_missing_or_incomplete_success_journal_is_not_a_final_answer(rows):
    with pytest.raises(ValueError, match="unavailable"):
        summarize_result(run(), rows)


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -1, "output_tokens": 1},
        {"input_tokens": "10", "output_tokens": 1},
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": 10},
    ],
)
def test_invalid_usage_fails_closed(usage):
    row = deepcopy(step())
    row["response"]["usage"] = usage
    with pytest.raises(ValueError):
        summarize_result(run(), [row])
