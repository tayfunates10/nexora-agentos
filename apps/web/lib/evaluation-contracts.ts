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
    raw_output: z.string().nullable(), source_agent_run_id: z.uuid().nullable(),
    baseline_passed: z.boolean().nullable(), regression: z.boolean(), improvement: z.boolean(),
  })),
}).refine(run => evalRunSummarySchema.safeParse(run).success
  && run.results.length === run.case_count, "Invalid evaluation result");

const judgeScore = z.number().int().min(0).max(4);
const judgeMilli = z.number().int().min(-1000).max(1000);
export const evalJudgeRunSchema = z.object({
  id: z.uuid(), workspace_id: z.uuid(), eval_run_id: z.uuid(),
  status: z.enum(["queued", "running", "succeeded", "failed"]),
  case_count: count.positive(), scored_count: count,
  judge_provider: z.string().nullable(), judge_model: z.string().nullable(),
  prompt_version: z.string().nullable(), error_code: z.string().nullable(),
  quality_milli: z.number().int().min(0).max(1000).nullable(),
  baseline_quality_milli: z.number().int().min(0).max(1000).nullable(),
  quality_delta_milli: judgeMilli.nullable(),
  regression_count: count, improvement_count: count,
  input_tokens: count, output_tokens: count, latency_ms: count,
  created_at: z.iso.datetime({ offset: true }), finished_at: z.iso.datetime({ offset: true }).nullable(),
  results: z.array(z.object({
    case_id: z.uuid(), case_key: z.string(),
    task_completion: judgeScore, answer_relevance: judgeScore, clarity: judgeScore,
    quality_milli: z.number().int().min(0).max(1000), rationale: z.string(),
    baseline_task_completion: judgeScore.nullable(),
    baseline_answer_relevance: judgeScore.nullable(), baseline_clarity: judgeScore.nullable(),
    baseline_quality_milli: z.number().int().min(0).max(1000).nullable(),
    quality_delta_milli: judgeMilli.nullable(), regression: z.boolean(), improvement: z.boolean(),
    input_tokens: count, output_tokens: count, latency_ms: count,
  })),
}).refine(
  run => run.scored_count <= run.case_count && run.results.length === run.scored_count
    && (run.status !== "succeeded" || run.scored_count === run.case_count)
    && run.regression_count <= run.scored_count && run.improvement_count <= run.scored_count,
  "Invalid judge result counts",
);

export type EvalJudgeRun = z.infer<typeof evalJudgeRunSchema>;

export type EvalRunSummary = z.infer<typeof evalRunSummarySchema>;
export type EvalRun = z.infer<typeof evalRunSchema>;
export type EvalSuite = z.infer<typeof evalSuiteSchema>;
