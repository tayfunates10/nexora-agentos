import { redirect } from "next/navigation";
import { z } from "zod";
import { currentSession } from "../../../../../../lib/server/session";
import { api } from "../../../../../../lib/server/api";
import { workspaceSchema } from "../../../../../../lib/workspace-contracts";
import { evalSuiteSchema, evalRunPageSchema } from "../../../../../../lib/evaluation-contracts";
import { EvaluationError, InvalidEvaluation, RunCard } from "../../shared";

export default async function SuiteDetail({ params, searchParams }: {
  params: Promise<{ id: string; suiteId: string }>; searchParams: Promise<{ cursor?: string }>;
}) {
  const { id, suiteId } = await params;
  const { cursor } = await searchParams;
  const session = await currentSession();
  if (!session) redirect("/login");
  if (![id, suiteId].every(value => z.uuid().safeParse(value).success)
    || (cursor !== undefined && !z.uuid().safeParse(cursor).success)) return <InvalidEvaluation back="/workspaces"/>;
  const root = `/workspaces/${id}/evaluations`;
  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    const suite = await api(session, `/api/v1/workspaces/${id}/eval-suites/${suiteId}`, evalSuiteSchema);
    let history;
    let historyError;
    if (workspace.role !== "member") {
      try {
        history = await api(session, `/api/v1/workspaces/${id}/eval-suites/${suiteId}/runs?limit=25${cursor ? "&cursor=" + cursor : ""}`, evalRunPageSchema);
      } catch (error) { historyError = error; }
    }
    return <section className="workspace-content evaluation-content">
      <a href={root}>← Evaluation suites</a>
      <p className="eyebrow">VERSION {suite.version} · {suite.case_count} CASES</p><h1>{suite.name}</h1>
      {suite.description && <p className="intro">{suite.description}</p>}
      <h2>Test cases</h2>
      <div className="eval-list">{suite.cases.map(item => <details className="card eval-card" key={item.id}>
        <summary>{item.case_no}. {item.case_key}</summary>
        <pre className="eval-evidence">{item.input}</pre>
        <dl className="eval-expectations">
          <dt>Expected tools</dt><dd>{item.expected_tools.join(", ") || "None required"}</dd>
          <dt>Forbidden tools</dt><dd>{item.forbidden_tools.join(", ") || "None specified"}</dd>
          <dt>Required citations</dt><dd>{item.expected_citations.join(", ") || "None required"}</dd>
        </dl>
      </details>)}</div>
      <h2 className="eval-section-title">Evaluation history</h2>
      {workspace.role === "member" && <p className="notice">Only workspace owners and admins can view evaluation results.</p>}
      {historyError !== undefined && <EvaluationError error={historyError} back={root + "/suites/" + suiteId}/>}
      {history && <>
        <p className="notice">Newest results first. A regression means a previously passing case now fails; an improvement means a previously failing case now passes.</p>
        {history.items.length === 0 ? <div className="empty"><h3>No evaluation runs on this page</h3><p>Submit observations for this suite through the evaluation API.</p></div>
          : <div className="eval-list">{history.items.map(run => <RunCard key={run.id} run={run} workspaceId={id}/>)}</div>}
        <nav className="pagination" aria-label="Evaluation history pages">
          {cursor && <a href={root + "/suites/" + suiteId}>Newest results</a>}
          {history.next_cursor && <a href={root + "/suites/" + suiteId + "?cursor=" + history.next_cursor}>Older results →</a>}
        </nav>
      </>}
    </section>;
  } catch (error) { return <EvaluationError error={error} back={root}/>; }
}
