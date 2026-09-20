import { redirect } from "next/navigation";
import { z } from "zod";
import { currentSession } from "../../../../lib/server/session";
import { api } from "../../../../lib/server/api";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { evalSuitePageSchema, evalTimestamp } from "../../../../lib/evaluation-contracts";
import { EvaluationError, InvalidEvaluation } from "./shared";

export default async function Evaluations({ params, searchParams }: {
  params: Promise<{ id: string }>; searchParams: Promise<{ cursor?: string }>;
}) {
  const { id } = await params;
  const { cursor } = await searchParams;
  const session = await currentSession();
  if (!session) redirect("/login");
  if (!z.uuid().safeParse(id).success || (cursor !== undefined && !z.uuid().safeParse(cursor).success))
    return <InvalidEvaluation back="/workspaces"/>;
  const root = `/workspaces/${id}/evaluations`;
  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    const suites = await api(session, `/api/v1/workspaces/${id}/eval-suites?limit=25${cursor ? "&cursor=" + cursor : ""}`, evalSuitePageSchema);
    return <section className="workspace-content evaluation-content">
      <a href={`/workspaces/${id}`}>← {workspace.name}</a>
      <p className="eyebrow">QUALITY / EVALUATIONS</p><h1>Evaluation suites</h1>
      <p className="intro">Inspect versioned test cases and compare saved results for your agents.</p>
      <p className="notice">These evaluations check submitted tool and citation observations. They do not run a model automatically.</p>
      {suites.items.length === 0 ? <div className="empty"><h2>No evaluation suites on this page</h2>
        <p>Create a versioned suite through the evaluation API to start tracking results.</p></div>
        : <div className="eval-list">{suites.items.map(suite => <article className="card eval-card" key={suite.id}>
          <p className="eyebrow">VERSION {suite.version} · {suite.case_count} CASES</p>
          <h2><a href={root + "/suites/" + suite.id}>{suite.name}</a></h2>
          {suite.description && <p>{suite.description}</p>}
          <p><time dateTime={suite.created_at}>{evalTimestamp(suite.created_at)}</time></p>
        </article>)}</div>}
      <nav className="pagination" aria-label="Evaluation suite pages">
        {cursor && <a href={root}>First page</a>}
        {suites.next_cursor && <a href={root + "?cursor=" + suites.next_cursor}>Next suites →</a>}
      </nav>
    </section>;
  } catch (error) { return <EvaluationError error={error} back={`/workspaces/${id}`}/>; }
}
