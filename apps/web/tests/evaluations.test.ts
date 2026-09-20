import test from "node:test";
import assert from "node:assert/strict";
import { evalRunSchema, evalRunPageSchema } from "../lib/evaluation-contracts.ts";

const id = "a2bdca9e-e07d-4b4a-ae19-6cc4e06e2d4c";
const summary = {
  id, workspace_id: id, suite_id: id, candidate_label: "candidate-v2",
  baseline_eval_run_id: id, case_count: 1, passed_count: 0, failed_count: 1,
  regression_count: 1, improvement_count: 0, created_at: "2026-09-20T13:00:00Z",
};
const result = {
  case_id: id, case_key: "grounded-read", passed: false, failures: ["missing_citation"],
  selected_tools: [], citations: [], raw_output: "  <script>alert(1)</script>\n",
  baseline_passed: true, regression: true, improvement: false,
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
