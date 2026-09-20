import test from "node:test";
import assert from "node:assert/strict";
import { evalJudgeRunSchema, evalRunSchema, evalRunPageSchema } from "../lib/evaluation-contracts.ts";

const id = "a2bdca9e-e07d-4b4a-ae19-6cc4e06e2d4c";
const summary = {
  id, workspace_id: id, suite_id: id, candidate_label: "candidate-v2",
  baseline_eval_run_id: id, case_count: 1, passed_count: 0, failed_count: 1,
  regression_count: 1, improvement_count: 0, created_at: "2026-09-20T13:00:00Z",
};
const result = {
  case_id: id, case_key: "grounded-read", passed: false, failures: ["missing_citation"],
  selected_tools: [], citations: [], raw_output: "  <script>alert(1)</script>\n",
  source_agent_run_id: id, baseline_passed: true, regression: true, improvement: false,
};
test("run detail contract preserves failed evidence exactly", () => {
  const run = evalRunSchema.parse({ ...summary, results: [result] });
  assert.equal(run.results[0].raw_output, result.raw_output);
});
test("history rejects invalid counts, timestamps and cursors", () => {
  for (const invalid of [
    { ...summary, failed_count: 0 }, { ...summary, regression_count: 2 },
    { ...summary, created_at: "yesterday" }, { ...summary, case_count: -1 },
  ]) assert.equal(evalRunPageSchema.safeParse({ items: [invalid], next_cursor: null }).success, false);
  assert.equal(evalRunPageSchema.safeParse({ items: [summary], next_cursor: "../other" }).success, false);
});
test("history contract excludes raw output and detail requires complete cases", () => {
  const page = evalRunPageSchema.parse({ items: [{ ...summary, results: [result], raw_output: "secret" }], next_cursor: null });
  assert.equal("raw_output" in page.items[0], false);
  assert.equal("results" in page.items[0], false);
  assert.equal(evalRunSchema.safeParse({ ...summary, results: [] }).success, false);
});

test("judge contract accepts partial progress and complete baseline deltas", () => {
  const caseScore = {
    case_id: id, case_key: "grounded-read", task_completion: 4, answer_relevance: 4,
    clarity: 4, quality_milli: 1000, rationale: "Strong answer.",
    baseline_task_completion: 1, baseline_answer_relevance: 1, baseline_clarity: 1,
    baseline_quality_milli: 250, quality_delta_milli: 750,
    regression: false, improvement: true, input_tokens: 20, output_tokens: 10, latency_ms: 45,
  };
  const partial = {
    id, workspace_id: id, eval_run_id: id, status: "running", case_count: 2, scored_count: 1,
    judge_provider: "openai", judge_model: "judge-model", prompt_version: "nexora-eval-judge-v1",
    error_code: null, quality_milli: 1000, baseline_quality_milli: 250, quality_delta_milli: 750,
    regression_count: 0, improvement_count: 1, input_tokens: 20, output_tokens: 10,
    latency_ms: 45, model_cost_usd_picos: null, model_cost_call_count: 0,
    model_cost_pricing_complete: false, model_cost_pricing_versions: [],
    created_at: "2026-09-20T13:00:00Z", finished_at: null, results: [caseScore],
  };
  assert.equal(evalJudgeRunSchema.parse(partial).scored_count, 1);
  assert.equal(evalJudgeRunSchema.safeParse({ ...partial, status: "succeeded" }).success, false);
  assert.equal(evalJudgeRunSchema.safeParse({ ...partial, scored_count: 2 }).success, false);
  assert.equal(
    evalJudgeRunSchema.safeParse({ ...partial, model_cost_usd_picos: "1.5" }).success,
    false,
  );
});
