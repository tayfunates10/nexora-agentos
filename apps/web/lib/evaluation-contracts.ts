import { z } from "zod";

const count = z.number().int().nonnegative();
export const evalSuiteSummarySchema = z.object({
  id: z.uuid(), workspace_id: z.uuid(), name: z.string(), version: count.positive(),
  description: z.string().nullable(), case_count: count.positive(), created_at: z.iso.datetime({ offset: true }),
});
export const evalSuitePageSchema = z.object({
  items: z.array(evalSuiteSummarySchema), next_cursor: z.uuid().nullable(),
});
export const evalSuiteSchema = evalSuiteSummarySchema.extend({
  cases: z.array(z.object({
    id: z.uuid(), case_no: count.positive(), case_key: z.string(), input: z.string(),
    expected_tools: z.array(z.string()), forbidden_tools: z.array(z.string()),
    expected_citations: z.array(z.string()),
  })),
});
const summaryShape = {
  id: z.uuid(), workspace_id: z.uuid(), suite_id: z.uuid(), candidate_label: z.string(),
  baseline_eval_run_id: z.uuid().nullable(), case_count: count.positive(),
  passed_count: count, failed_count: count, regression_count: count, improvement_count: count,
  created_at: z.iso.datetime({ offset: true }),
};
export const evalRunSummarySchema = z.object(summaryShape).refine(
  run => run.passed_count + run.failed_count === run.case_count
    && run.regression_count <= run.failed_count && run.improvement_count <= run.passed_count,
  "Invalid evaluation counts",
);
export const evalRunPageSchema = z.object({
  items: z.array(evalRunSummarySchema), next_cursor: z.uuid().nullable(),
});
export const evalRunSchema = z.object({
  ...summaryShape,
  results: z.array(z.object({
    case_id: z.uuid(), case_key: z.string(), passed: z.boolean(),
    failures: z.array(z.string()), selected_tools: z.array(z.string()), citations: z.array(z.string()),
    raw_output: z.string().nullable(), baseline_passed: z.boolean().nullable(),
    regression: z.boolean(), improvement: z.boolean(),
  })),
}).refine(run => evalRunSummarySchema.safeParse(run).success
  && run.results.length === run.case_count, "Invalid evaluation result");

export type EvalRunSummary = z.infer<typeof evalRunSummarySchema>;
export type EvalRun = z.infer<typeof evalRunSchema>;
export type EvalSuite = z.infer<typeof evalSuiteSchema>;
export function evalTimestamp(value: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium", timeStyle: "short", timeZone: "UTC",
  }).format(new Date(value)) + " UTC";
}
