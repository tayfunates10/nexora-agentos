import { redirect } from "next/navigation";
import { z } from "zod";
import {
  RUN_STATUS_LABELS,
  runDuration,
  runPageSchema,
  runStatusSchema,
  runTimestamp,
} from "../../../../lib/agent-contracts";
import { api } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { ConsoleError, InvalidLink, RunStatusBadge } from "../console";

export default async function Runs({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string; status?: string; mine?: string }>;
}) {
  const { id } = await params;
  const query = await searchParams;
  const session = await currentSession();
  if (!session) redirect("/login");

  const status = query.status ? runStatusSchema.safeParse(query.status) : null;
  const mine = query.mine === "1";
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !z.uuid().safeParse(query.cursor).success)
    || (status !== null && !status.success)
    || (query.mine !== undefined && query.mine !== "1")) {
    return <InvalidLink back={`/workspaces/${id}`} backLabel="Back to workspace"/>;
  }

  const root = `/workspaces/${id}/runs`;
  const filters = (status?.success ? `&status=${status.data}` : "") + (mine ? "&mine=1" : "");
  const link = (extra: string) => root + (filters || extra ? "?" + (filters + extra).slice(1) : "");
  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    const runs = await api(
      session,
      `/api/v1/workspaces/${id}/runs?limit=25`
        + (status?.success ? `&status=${status.data}` : "")
        + (mine ? "&requested_by_me=true" : "")
        + (query.cursor ? "&cursor=" + query.cursor : ""),
      runPageSchema,
    );

    return <section className="workspace-content evaluation-content">
      <a href={`/workspaces/${id}`}>← {workspace.name}</a>
      <p className="eyebrow">EXECUTION / HISTORY</p><h1>Agent runs</h1>
      <p className="intro">
        Every run this workspace has started, newest first. Answers stay with the person who
        started the run; this history shows status, timing and which agent ran.
      </p>
      <div className="section-head">
        <p className="notice">Runs update as the worker progresses.</p>
        <a href={link("")}>Refresh ↗</a>
      </div>

      <nav className="spend-filters" aria-label="Filter runs">
        <a href={root + (mine ? "?mine=1" : "")} aria-current={status === null ? "page" : undefined}>
          All statuses
        </a>
        {runStatusSchema.options.map(option => <a
          key={option}
          href={`${root}?status=${option}` + (mine ? "&mine=1" : "")}
          aria-current={status?.success && status.data === option ? "page" : undefined}
        >{RUN_STATUS_LABELS[option]}</a>)}
        <a
          href={mine ? root + (status?.success ? `?status=${status.data}` : "")
            : `${root}?mine=1` + (status?.success ? `&status=${status.data}` : "")}
          aria-current={mine ? "page" : undefined}
        >{mine ? "Started by me ✓" : "Started by me"}</a>
      </nav>

      {runs.items.length === 0
        ? <div className="empty"><h2>No runs on this page</h2>
          <p>Start one from the <a href={`/workspaces/${id}/agents`}>agents page</a>. A queued run
            executes only when an agent worker is configured and running.</p></div>
        : <table className="spend-table run-table">
          <caption>Run history for this workspace. Each row links to its execution timeline.</caption>
          <thead><tr>
            <th scope="col">Started</th><th scope="col">Agent</th><th scope="col">Status</th>
            <th scope="col">Attempts</th><th scope="col">Duration</th><th scope="col">Run</th>
          </tr></thead>
          <tbody>{runs.items.map(run => <tr key={run.id}>
            <td data-label="Started">
              <time dateTime={run.created_at}>{runTimestamp(run.created_at)}</time></td>
            <td data-label="Agent">{run.agent_name}</td>
            <td data-label="Status">
              <RunStatusBadge status={run.status}/>
              {run.failure_code && <span className="run-code"><code>{run.failure_code}</code></span>}
            </td>
            <td data-label="Attempts">{run.attempt_count}</td>
            <td data-label="Duration">{runDuration(run) ?? "—"}</td>
            <td data-label="Run">
              <a href={`${root}/${run.id}`}>Open{run.requested_by_me ? " · yours" : ""}</a></td>
          </tr>)}</tbody>
        </table>}

      <nav className="pagination" aria-label="Run history pages">
        {query.cursor && <a href={link("")}>Newest runs</a>}
        {runs.next_cursor && <a href={link(`&cursor=${runs.next_cursor}`)}>Older runs →</a>}
      </nav>
    </section>;
  } catch (error) {
    return <ConsoleError
      error={error} area="Runs" back={`/workspaces/${id}`} backLabel="Back to workspace"
    />;
  }
}
