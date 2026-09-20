from pydantic import BaseModel, ConfigDict, Field

from nexora_api.agents import AgentRunResult, RecordedModelStep


class _Usage(BaseModel):
    model_config = ConfigDict(strict=True)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class _ToolSelection(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class _Response(BaseModel):
    text: str | None = Field(max_length=100000)
    tool_calls: list[_ToolSelection] = Field(max_length=16)
    usage: _Usage
    finish_reason: str


def summarize_result(run, rows) -> AgentRunResult:
    """Expose final text only after successful execution; never expose call arguments."""
    if run["status"] not in ("succeeded", "failed", "cancelled"):
        raise ValueError("run_result_not_ready")
    if len(rows) > 32 or [row["step_no"] for row in rows] != list(range(len(rows))):
        raise ValueError("run_result_unavailable")
    steps = []
    selections = set()
    last = None
    for row in rows:
        response = _Response.model_validate(row["response"])
        selections.update(call.name for call in response.tool_calls)
        steps.append(
            RecordedModelStep(
                step_no=row["step_no"],
                provider=row["provider"],
                model=row["model"],
                finish_reason=response.finish_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        )
        last = response

    output = None
    reason = None
    if run["status"] == "succeeded":
        if last is None or last.finish_reason not in ("stop", "refusal") or last.tool_calls:
            raise ValueError("run_result_unavailable")
        if not last.text:
            raise ValueError("run_result_unavailable")
        output, reason = last.text, last.finish_reason

    return AgentRunResult(
        run_id=run["id"],
        workspace_id=run["workspace_id"],
        agent_id=run["agent_id"],
        trace_id=run["trace_id"],
        status=run["status"],
        output_text=output,
        finish_reason=reason,
        failure_code=run["failure_code"],
        recorded_input_tokens=sum(step.input_tokens for step in steps),
        recorded_output_tokens=sum(step.output_tokens for step in steps),
        selected_tools=sorted(selections),
        model_steps=steps,
    )
