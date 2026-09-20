import { redirect } from "next/navigation";
import { ApiError } from "../../../../lib/server/api";
import { evalTimestamp, type EvalRunSummary } from "../../../../lib/evaluation-contracts";

export function EvaluationError({ error, back }: { error: unknown; back: string }) {
  if (error instanceof ApiError && error.status === 401) redirect("/login?error=session_expired");
  const denied = error instanceof ApiError && [403, 404].includes(error.status);
  return <section className="workspace-content evaluation-content">
    <h1>{denied ? "Evaluation not found or access denied" : "Evaluations unavailable"}</h1>
    <p role="alert">{denied ? "Check your workspace access and the requested evaluation." : "The evaluation service could not be reached. Try again shortly."}</p>
    <a href={back}>Back to evaluations</a>
  </section>;
}

export function InvalidEvaluation({ back }: { back: string }) {
  return <section className="workspace-content"><h1>Invalid evaluation link</h1>
    <p role="alert">The requested identifier or page cursor is invalid.</p><a href={back}>Back</a>
  </section>;
}

export function RunCounts({ run }: { run: EvalRunSummary }) {
  return <dl className="eval-counts">
    <div><dt>Passed</dt><dd>{run.passed_count} / {run.case_count}</dd></div>
    <div><dt>Failed</dt><dd>{run.failed_count}</dd></div>
    <div><dt>Regressions</dt><dd>{run.baseline_eval_run_id ? run.regression_count : "—"}</dd></div>
    <div><dt>Improvements</dt><dd>{run.baseline_eval_run_id ? run.improvement_count : "—"}</dd></div>
  </dl>;
}

export function RunCard({ run, workspaceId }: { run: EvalRunSummary; workspaceId: string }) {
  return <article className="card eval-card">
    <h3><a href={`/workspaces/${workspaceId}/evaluations/runs/${run.id}`}>{run.candidate_label}</a></h3>
    <p><time dateTime={run.created_at}>{evalTimestamp(run.created_at)}</time></p>
    <RunCounts run={run}/>
    <p>{run.baseline_eval_run_id ? "Compared with a saved baseline." : "No baseline comparison."}</p>
  </article>;
}
