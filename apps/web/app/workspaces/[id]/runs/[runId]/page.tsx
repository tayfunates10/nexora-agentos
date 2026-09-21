import { redirect } from "next/navigation";
import { z } from "zod";
import {
  RUN_STATUS_HELP,
  TERMINAL_STATUSES,
  eventDetail,
  runDuration,
  runEventLabel,
  runEventPageSchema,
  runResultSchema,
  runSchema,
  runTimestamp,
  type RunResult,
} from "../../../../../lib/agent-contracts";
import { api, ApiError } from "../../../../../lib/server/api";
import { currentSession } from "../../../../../lib/server/session";
import { workspaceSchema } from "../../../../../lib/workspace-contracts";
import { ConsoleError, Failed, InvalidLink, RunStatusBadge, Saved } from "../../console";

const MESSAGES: Record<string, string> = {
  forbidden: "Only the person who started this run, or a workspace owner or admin, can cancel it.",
  failed: "The cancellation could not be recorded. Refresh the run and try again.",
};

// The answer belongs to the requester (retrieval evidence can be ACL-bound to them), and it
// only exists once the run is terminal. Both refusals are states this page shows, not errors.
type ResultState =
  | { kind: "result"; result: RunResult }
  | { kind: "not_requester" }
  | { kind: "not_ready" }
  | { kind: "pending" };

export default async function RunDetail({ params, searchParams }: {
  params: Promise<{ id: string; runId: string }>;
  searchParams: Promise<{ cancelled?: string; error?: string }>;
}) {
  const { id, runId } = await params;
  const query = await searchParams;
  const session = await currentSession();
  if (!session) redirect("/login");
  if (!z.uuid().safeParse(id).success || !z.uuid().safeParse(runId).success) {
    return <InvalidLink back={`/workspaces/${id}/runs`} backLabel="Back to runs"/>;
  }

  const base = `/api/v1/workspaces/${id}/runs/${runId}`;
  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    const run = await api(session, base, runSchema);
    const events = await api(session, base + "/events?limit=100", runEventPageSchema);
    const terminal = TERMINAL_STATUSES.includes(run.status);

    let result: ResultState = { kind: "pending" };
    if (terminal) {
      try {
        result = { kind: "result", result: await api(session, base + "/result", runResultSchema) };
      } catch (error) {
        if (!(error instanceof ApiError)) throw error;
        if (error.status === 404) result = { kind: "not_requester" };
        else if (error.status === 409) result = { kind: "not_ready" };
        else throw error;
      }
    }
    const canCancel = !terminal && run.cancel_requested_at === null;

    return <section className="workspace-content evaluation-content">
      <a href={`/workspaces/${id}/runs`}>← Runs in {workspace.name}</a>
      <p className="eyebrow">EXECUTION / RUN</p><h1>Agent run</h1>
      <Saved message={query.cancelled ? "Cancellation was recorded for this run." : null}/>
      <Failed message={query.error ? MESSAGES[query.error] ?? MESSAGES.failed : null}/>

      <div className="card run-panel">
        <RunStatusBadge status={run.status}/>
        <p className="notice">{RUN_STATUS_HELP[run.status]}</p>
        <dl className="eval-counts run-counts">
          <div><dt>Started</dt>
            <dd><time dateTime={run.created_at}>{runTimestamp(run.created_at)}</time></dd></div>
          <div><dt>Attempts</dt><dd>{run.attempt_count}</dd></div>
          <div><dt>Duration</dt><dd>{runDuration(run) ?? "—"}</dd></div>
          <div><dt>Events</dt><dd>{events.items.length}{events.next_cursor ? "+" : ""}</dd></div>
        </dl>
        <dl className="eval-expectations">
          <dt>Run</dt><dd><code>{run.id}</code></dd>
          <dt>Trace</dt><dd><code>{run.trace_id}</code></dd>
          <dt>Agent</dt><dd><a href={`/workspaces/${id}/agents`}><code>{run.agent_id}</code></a></dd>
          {run.failure_code && <><dt>Failure code</dt><dd><code>{run.failure_code}</code></dd></>}
          {run.cancel_requested_at && <><dt>Cancellation requested</dt>
            <dd><time dateTime={run.cancel_requested_at}>
              {runTimestamp(run.cancel_requested_at)}</time></dd></>}
        </dl>
        <div className="section-head">
          <p className="notice">This page reflects the run as of the last page load.</p>
          <a href={`/workspaces/${id}/runs/${runId}`}>Refresh ↗</a>
        </div>
        {run.status === "waiting_for_approval" && <p role="alert">
          This run is paused on a tool approval. Owners and admins decide it on the{" "}
          <a href={`/workspaces/${id}/approvals`}>approvals page</a>; nothing executes until then.
        </p>}
        {canCancel && <form className="workspace-form run-cancel" action="/workspaces/runs/cancel" method="post">
          <input type="hidden" name="csrf" value={session.csrf}/>
          <input type="hidden" name="workspace" value={id}/>
          <input type="hidden" name="run" value={runId}/>
          <p>
            Cancelling closes any pending approval for this run. A run already executing stops at
            its next checkpoint; work already committed is not undone.
          </p>
          <button type="submit">Cancel this run</button>
        </form>}
        {!terminal && run.cancel_requested_at !== null && <p className="notice">
          Cancellation has been requested. The worker finishes its current step and then stops.
        </p>}
      </div>

      <h2 className="eval-section-title">Result</h2>
      {result.kind === "pending" && <div className="empty"><h3>No result yet</h3>
        <p>A final answer is published only once the run reaches a terminal status.</p></div>}
      {result.kind === "not_requester" && <div className="empty"><h3>Result not available to you</h3>
        <p>Only the person who started this run can read its answer, because a run can include
          retrieved content that is shared with them alone. Workspace admin does not override it.</p></div>}
      {result.kind === "not_ready" && <div className="empty"><h3>No publishable result</h3>
        <p>This run ended without a complete recorded answer. The timeline below still shows what
          happened.</p></div>}
      {result.kind === "result" && <div className="card run-panel">
        {result.result.output_text === null
          ? <p className="notice">
            This run published no answer. Failed and cancelled runs never return partial output.
          </p>
          : <>
            <p className="notice">
              Final answer{result.result.finish_reason === "refusal"
                ? " · the model refused the task" : ""}, recorded by the worker.
            </p>
            <pre className="eval-evidence">{result.result.output_text}</pre>
          </>}
        <dl className="eval-counts run-counts">
          <div><dt>Model steps</dt><dd>{result.result.model_steps.length}</dd></div>
          <div><dt>Input tokens</dt>
            <dd>{result.result.recorded_input_tokens.toLocaleString("en-GB")}</dd></div>
          <div><dt>Output tokens</dt>
            <dd>{result.result.recorded_output_tokens.toLocaleString("en-GB")}</dd></div>
          <div><dt>Tools selected</dt><dd>{result.result.selected_tools.length}</dd></div>
        </dl>
        {result.result.selected_tools.length > 0 && <p className="notice">
          Selected by the model: {result.result.selected_tools.map((tool, index) =>
            <span key={tool}>{index > 0 ? ", " : ""}<code>{tool}</code></span>)}.
          Selection is not execution: policy and approval decide that.
        </p>}
        {result.result.model_steps.length > 0 && <table className="spend-table">
          <caption>Recorded model steps. Token counts cover persisted responses only.</caption>
          <thead><tr>
            <th scope="col">Step</th><th scope="col">Model</th><th scope="col">Finish</th>
            <th scope="col">Input</th><th scope="col">Output</th>
          </tr></thead>
          <tbody>{result.result.model_steps.map(step => <tr key={step.step_no}>
            <td data-label="Step">{step.step_no}</td>
            <td data-label="Model">{step.provider}/{step.model}</td>
            <td data-label="Finish">{step.finish_reason}</td>
            <td data-label="Input">{step.input_tokens.toLocaleString("en-GB")}</td>
            <td data-label="Output">{step.output_tokens.toLocaleString("en-GB")}</td>
          </tr>)}</tbody>
        </table>}
      </div>}

      <h2 className="eval-section-title">Execution timeline</h2>
      {events.items.length === 0
        ? <div className="empty"><h3>No events recorded</h3>
          <p>Run events are append-only. An empty timeline means nothing has been written yet.</p></div>
        : <ol className="run-timeline">{events.items.map(event => {
          const detail = eventDetail(event.payload);
          return <li key={event.id}>
            <p className="run-event-head">
              <strong>{runEventLabel(event.event_type)}</strong>
              <time dateTime={event.created_at}>{runTimestamp(event.created_at)}</time>
            </p>
            <p className="run-event-type"><code>{event.event_type}</code></p>
            {detail && <p className="notice">{detail}</p>}
          </li>;
        })}</ol>}
      {events.next_cursor && <p className="notice">
        Showing the first 100 events of this run.
      </p>}
    </section>;
  } catch (error) {
    return <ConsoleError
      error={error} area="Run" back={`/workspaces/${id}/runs`} backLabel="Back to runs"
    />;
  }
}
