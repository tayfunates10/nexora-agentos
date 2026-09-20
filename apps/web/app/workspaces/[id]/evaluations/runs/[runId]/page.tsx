import { randomUUID } from "node:crypto";
import { redirect } from "next/navigation";
import { z } from "zod";
import {
  evalJudgeRunSchema,
  evalRunSchema,
  evalTimestamp,
  type EvalJudgeRun,
} from "../../../../../../lib/evaluation-contracts";
import { api, ApiError } from "../../../../../../lib/server/api";
import { currentSession } from "../../../../../../lib/server/session";
import { EvaluationError, InvalidEvaluation, RunCounts } from "../../shared";

function quality(value: number | null): string {
  return value === null ? "—" : (value / 10).toFixed(1) + "%";
}

function delta(value: number | null): string {
  if (value === null) return "—";
  const points = value / 10;
  return (points > 0 ? "+" : "") + points.toFixed(1) + " pts";
}

function usdFromPicos(value: string | null, complete: boolean): string {
  if (!complete || value === null) return "Unpriced";
  const picos = BigInt(value);
  const whole = picos / 1_000_000_000_000n;
  const fraction = (picos % 1_000_000_000_000n)
    .toString()
    .padStart(12, "0")
    .replace(/0+$/, "");
  return "$" + whole + (fraction ? "." + fraction : "");
}

function JudgeSummary({ judge }: { judge: EvalJudgeRun }) {
  const stateClass = judge.status === "succeeded"
    ? "state up"
    : judge.status === "failed" ? "state down" : "state unknown";
  return <>
    <p className={stateClass}>Judge {judge.status}</p>
    <dl className="eval-counts judge-counts">
      <div><dt>Quality</dt><dd>{quality(judge.quality_milli)}</dd></div>
      <div><dt>Scored</dt><dd>{judge.scored_count} / {judge.case_count}</dd></div>
      <div><dt>Baseline</dt><dd>{quality(judge.baseline_quality_milli)}</dd></div>
      <div><dt>Quality delta</dt><dd>{delta(judge.quality_delta_milli)}</dd></div>
    </dl>
    {judge.judge_model && <p className="notice">
      Pinned judge: {judge.judge_provider}/{judge.judge_model} · {judge.prompt_version}
    </p>}
    <p className="notice">
      Model cost: {usdFromPicos(
        judge.model_cost_usd_picos,
        judge.model_cost_pricing_complete,
      )} · provider responses: {judge.model_cost_call_count}
      {judge.model_cost_pricing_versions.length > 0
        ? " · pricing: " + judge.model_cost_pricing_versions.join(", ")
        : ""}
    </p>
    {judge.status === "succeeded" && <p className="notice">
      Judge regressions: {judge.regression_count} · improvements: {judge.improvement_count}
      {" · "}tokens: {judge.input_tokens + judge.output_tokens}
      {" · "}provider latency: {judge.latency_ms} ms
    </p>}
  </>;
}

export default async function RunDetail({
  params,
  searchParams,
}: {
  params: Promise<{ id: string; runId: string }>;
  searchParams: Promise<{ judge?: string; judge_error?: string }>;
}) {
  const { id, runId } = await params;
  const query = await searchParams;
  const session = await currentSession();
  if (!session) redirect("/login");
  if (![id, runId].every(value => z.uuid().safeParse(value).success)) {
    return <InvalidEvaluation back="/workspaces"/>;
  }

  const root = `/workspaces/${id}/evaluations`;
  try {
    const run = await api(
      session,
      `/api/v1/workspaces/${id}/eval-runs/${runId}`,
      evalRunSchema,
    );
    let judge: EvalJudgeRun | null = null;
    let judgeUnavailable = false;
    try {
      judge = await api(
        session,
        `/api/v1/workspaces/${id}/eval-runs/${runId}/judge-runs/latest`,
        evalJudgeRunSchema,
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) throw error;
      if (!(error instanceof ApiError && error.status === 404)) judgeUnavailable = true;
    }

    const judgeEligible = run.results.every(result => result.source_agent_run_id !== null);
    const judgeScores = new Map((judge?.results ?? []).map(result => [result.case_id, result]));
    const canQueue = !judgeUnavailable
      && judgeEligible
      && (judge === null || judge.status === "failed");

    return <section className="workspace-content evaluation-content">
      <a href={root + "/suites/" + run.suite_id}>← Suite and history</a>
      <p className="eyebrow">SAVED EVALUATION</p><h1>{run.candidate_label}</h1>
      <p><time dateTime={run.created_at}>{evalTimestamp(run.created_at)}</time></p>
      <RunCounts run={run}/>
      {run.baseline_eval_run_id
        ? <p><a href={root + "/runs/" + run.baseline_eval_run_id}>View baseline evaluation →</a></p>
        : <p className="notice">No baseline was selected for this evaluation.</p>}

      <section className="card judge-panel" aria-labelledby="quality-judge-heading">
        <p className="eyebrow">AI QUALITY JUDGE · PROBABILISTIC</p>
        <h2 id="quality-judge-heading">Answer quality</h2>
        <p className="notice">
          This optional worker-side judge scores task completion, relevance and clarity.
          It never changes the deterministic pass/fail result above.
        </p>
        {query.judge === "queued" && <p role="status">The quality judge was queued.</p>}
        {query.judge_error === "unavailable" && <p role="alert">
          This evaluation cannot be judged automatically because it is not a complete agent-run import.
        </p>}
        {query.judge_error === "failed" && <p role="alert">
          The judge could not be queued. Check your current access and try again.
        </p>}
        {judgeUnavailable && <p role="alert">
          Judge status is temporarily unavailable. Deterministic evaluation results remain available.
        </p>}
        {judge && <JudgeSummary judge={judge}/>}
        {judge?.status === "failed" && <p role="alert">
          Judge failed with code <code>{judge.error_code ?? "unknown"}</code>. No deterministic result changed.
        </p>}
        {judge && ["queued", "running"].includes(judge.status) && <p>
          <a href={root + "/runs/" + runId}>Refresh judge status</a>
        </p>}
        {!judgeEligible && judge === null && <p className="notice">
          Quality judging is available only for evaluations imported from completed agent runs.
        </p>}
        {canQueue && <form className="judge-form" action="/workspaces/evaluations/judge" method="post">
          <input type="hidden" name="csrf" value={session.csrf}/>
          <input type="hidden" name="workspace" value={id}/>
          <input type="hidden" name="eval_run" value={runId}/>
          <input type="hidden" name="idempotency_key" value={randomUUID()}/>
          <button type="submit">{judge?.status === "failed" ? "Retry quality judge" : "Run quality judge"}</button>
        </form>}
      </section>

      <h2>Case results</h2>
      <div className="eval-list">{run.results.map(result => {
        const score = judgeScores.get(result.case_id);
        return <article className="card eval-card" key={result.case_id}>
          <p className={result.passed ? "state up" : "state down"}>
            {result.passed ? "Passed" : "Failed"}
            {result.regression ? " · Regression" : result.improvement ? " · Improvement" : ""}
          </p>
          <h3>{result.case_key}</h3>
          {result.baseline_passed !== null
            && <p>Baseline: {result.baseline_passed ? "passed" : "failed"}</p>}
          {result.failures.length > 0
            && <ul>{result.failures.map((failure, index) => <li key={index}>{failure}</li>)}</ul>}
          <dl className="eval-expectations">
            <dt>Selected tools</dt><dd>{result.selected_tools.join(", ") || "None"}</dd>
            <dt>Citations</dt><dd>{result.citations.join(", ") || "None"}</dd>
          </dl>
          {score && <div className="judge-case">
            <p className={score.regression ? "state down" : score.improvement ? "state up" : "state unknown"}>
              AI judge · {quality(score.quality_milli)}
              {score.regression ? " · Quality regression" : score.improvement ? " · Quality improvement" : ""}
            </p>
            <dl className="eval-expectations">
              <dt>Task completion</dt><dd>{score.task_completion} / 4</dd>
              <dt>Relevance</dt><dd>{score.answer_relevance} / 4</dd>
              <dt>Clarity</dt><dd>{score.clarity} / 4</dd>
              {score.baseline_quality_milli !== null && <>
                <dt>Baseline quality</dt><dd>{quality(score.baseline_quality_milli)}</dd>
                <dt>Quality delta</dt><dd>{delta(score.quality_delta_milli)}</dd>
              </>}
              <dt>Rationale</dt><dd>{score.rationale}</dd>
            </dl>
          </div>}
          {!result.passed && (result.raw_output !== null
            ? <details><summary>Failed output</summary>
              <pre className="eval-evidence">{result.raw_output}</pre></details>
            : <p className="notice">No raw output was supplied for this failed case.</p>)}
        </article>;
      })}</div>
    </section>;
  } catch (error) {
    return <EvaluationError error={error} back={root}/>;
  }
}
