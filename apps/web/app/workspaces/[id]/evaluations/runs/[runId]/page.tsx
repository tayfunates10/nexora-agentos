import { redirect } from "next/navigation";
import { z } from "zod";
import { currentSession } from "../../../../../../lib/server/session";
import { api } from "../../../../../../lib/server/api";
import { evalRunSchema, evalTimestamp } from "../../../../../../lib/evaluation-contracts";
import { EvaluationError, InvalidEvaluation, RunCounts } from "../../shared";

export default async function RunDetail({ params }: { params: Promise<{ id: string; runId: string }> }) {
  const { id, runId } = await params;
  const session = await currentSession();
  if (!session) redirect("/login");
  if (![id, runId].every(value => z.uuid().safeParse(value).success)) return <InvalidEvaluation back="/workspaces"/>;
  const root = `/workspaces/${id}/evaluations`;
  try {
    const run = await api(session, `/api/v1/workspaces/${id}/eval-runs/${runId}`, evalRunSchema);
    return <section className="workspace-content evaluation-content">
      <a href={root + "/suites/" + run.suite_id}>← Suite and history</a>
      <p className="eyebrow">SAVED EVALUATION</p><h1>{run.candidate_label}</h1>
      <p><time dateTime={run.created_at}>{evalTimestamp(run.created_at)}</time></p>
      <RunCounts run={run}/>
      {run.baseline_eval_run_id ? <p><a href={root + "/runs/" + run.baseline_eval_run_id}>View baseline evaluation →</a></p> : <p className="notice">No baseline was selected for this evaluation.</p>}
      <h2>Case results</h2>
      <div className="eval-list">{run.results.map(result => <article className="card eval-card" key={result.case_id}>
        <p className={result.passed ? "state up" : "state down"}>
          {result.passed ? "Passed" : "Failed"}{result.regression ? " · Regression" : result.improvement ? " · Improvement" : ""}
        </p>
        <h3>{result.case_key}</h3>
        {result.baseline_passed !== null && <p>Baseline: {result.baseline_passed ? "passed" : "failed"}</p>}
        {result.failures.length > 0 && <ul>{result.failures.map((failure, index) => <li key={index}>{failure}</li>)}</ul>}
        <dl className="eval-expectations">
          <dt>Selected tools</dt><dd>{result.selected_tools.join(", ") || "None"}</dd>
          <dt>Citations</dt><dd>{result.citations.join(", ") || "None"}</dd>
        </dl>
        {!result.passed && (result.raw_output !== null
          ? <details><summary>Failed output</summary><pre className="eval-evidence">{result.raw_output}</pre></details>
          : <p className="notice">No raw output was supplied for this failed case.</p>)}
      </article>)}</div>
    </section>;
  } catch (error) { return <EvaluationError error={error} back={root}/>; }
}
