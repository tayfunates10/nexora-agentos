import { redirect } from "next/navigation";
import { z } from "zod";
import {
  TERMINAL_STATUSES,
  eventDetail,
  runDurationSeconds,
  runEventKey,
  runEventPageSchema,
  runResultSchema,
  runSchema,
  runStatusHelpKey,
  type RunResult,
} from "../../../../../lib/agent-contracts";
import { api, ApiError } from "../../../../../lib/server/api";
import { currentSession } from "../../../../../lib/server/session";
import { consoleChrome } from "../../../../../lib/server/chrome";
import { workspaceSchema } from "../../../../../lib/workspace-contracts";
import {
  formatDuration, formatNumber, formatTimestamp,
} from "../../../../../lib/i18n/format.ts";
import { ConsoleBreadcrumb, ConsoleShell } from "../../../../../components/shell/ConsoleShell.tsx";
import {
  CodeBlock, Detail, DetailList, EmptyState, Hint, Metric, Metrics, Notice,
  PageHeader, Panel, RefreshLink, SectionHead,
} from "../../../../../components/ui/primitives.tsx";
import { TextWithLink } from "../../../../../components/ui/RichText.tsx";
import { ConsoleProblem, Failed, InvalidLinkPage, RunStatusBadge, Saved } from "../../console";
import type { MessageKey } from "../../../../../messages/en.ts";

const MESSAGES: Record<string, MessageKey> = {
  forbidden: "runDetail.error.forbidden",
  failed: "runDetail.error.failed",
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
  const [{ id, runId }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  let chrome = await consoleChrome(session);
  if (!z.uuid().safeParse(id).success || !z.uuid().safeParse(runId).success) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: `/workspaces/${id}/runs`, label: chrome.ui.t("runs.backToRuns"),
    }}/>;
  }

  const base = `/api/v1/workspaces/${id}/runs/${runId}`;
  const runsRoot = `/workspaces/${id}/runs`;
  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
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
    // Cancellation is offered only where it can still do something. The API decides who
    // may actually cancel; hiding the button never stands in for that check.
    const canCancel = !terminal && run.cancel_requested_at === null;
    const seconds = runDurationSeconds(run);

    return <ConsoleShell
      chrome={chrome}
      active="runs"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace} trail={[{ href: runsRoot, label: ui.t("navigation.runs") }]}
      />}
    >
      <PageHeader eyebrow={ui.t("runDetail.eyebrow")} title={ui.t("runDetail.title")}/>
      <Saved message={query.cancelled ? ui.t("runDetail.cancelRecorded") : null}/>
      <Failed message={query.error ? ui.t(MESSAGES[query.error] ?? MESSAGES.failed) : null}/>

      <Panel label={ui.t("runDetail.summaryLabel")} testId="run-summary">
        <RunStatusBadge chrome={chrome} status={run.status}/>
        <p className="notice">{ui.t(runStatusHelpKey(run.status))}</p>
        <Metrics>
          <Metric label={ui.t("runDetail.metric.started")} value={
            <time dateTime={run.created_at}>{formatTimestamp(run.created_at, ui.locale)}</time>
          }/>
          <Metric
            label={ui.t("runDetail.metric.attempts")}
            value={formatNumber(run.attempt_count, ui.locale)}
          />
          <Metric
            label={ui.t("runDetail.metric.duration")}
            value={seconds === null ? ui.t("common.empty") : formatDuration(seconds, ui.t)}
          />
          <Metric
            label={ui.t("runDetail.metric.events")}
            value={formatNumber(events.items.length, ui.locale) + (events.next_cursor ? "+" : "")}
          />
        </Metrics>
        {/* Identifiers are the API's own; they are shown verbatim in both languages. */}
        <DetailList>
          <Detail label={ui.t("runDetail.identity.run")}><code>{run.id}</code></Detail>
          <Detail label={ui.t("runDetail.identity.trace")}><code>{run.trace_id}</code></Detail>
          <Detail label={ui.t("runDetail.identity.agent")}>
            <a className="link" href={`/workspaces/${id}/agents`}><code>{run.agent_id}</code></a>
          </Detail>
          {run.failure_code && <Detail label={ui.t("runDetail.identity.failureCode")}>
            <code>{run.failure_code}</code></Detail>}
          {run.cancel_requested_at && <Detail label={ui.t("runDetail.identity.cancelRequested")}>
            <time dateTime={run.cancel_requested_at}>
              {formatTimestamp(run.cancel_requested_at, ui.locale)}</time></Detail>}
        </DetailList>
        <div className="section-head">
          <p className="notice">{ui.t("runDetail.asOfNotice")}</p>
          <RefreshLink href={`${runsRoot}/${runId}`} label={ui.t("common.refresh")}/>
        </div>
        {run.status === "waiting_for_approval" && <Notice live="alert" tone="warning">
          <TextWithLink
            ui={ui} message="runDetail.waitingApproval" link="runDetail.waitingApprovalLink"
            href={`/workspaces/${id}/approvals`}
          />
        </Notice>}
        {canCancel && <form className="form" action="/workspaces/runs/cancel" method="post">
          <input type="hidden" name="csrf" value={session.csrf}/>
          <input type="hidden" name="workspace" value={id}/>
          <input type="hidden" name="run" value={runId}/>
          <Notice tone="danger">{ui.t("runDetail.cancelExplain")}</Notice>
          <div>
            <button type="submit" className="button danger">{ui.t("runDetail.cancelSubmit")}</button>
          </div>
        </form>}
        {!terminal && run.cancel_requested_at !== null
          && <Hint>{ui.t("runDetail.cancelPending")}</Hint>}
      </Panel>

      <SectionHead id="run-result-title" title={ui.t("runDetail.resultTitle")}/>
      {result.kind !== "result" && <EmptyState title={ui.t(
        result.kind === "pending" ? "runDetail.result.pendingTitle"
          : result.kind === "not_requester" ? "runDetail.result.notRequesterTitle"
          : "runDetail.result.notReadyTitle",
      )}>
        <p>{ui.t(
          result.kind === "pending" ? "runDetail.result.pendingBody"
            : result.kind === "not_requester" ? "runDetail.result.notRequesterBody"
            : "runDetail.result.notReadyBody",
        )}</p>
      </EmptyState>}
      {result.kind === "result" && <Panel labelledBy="run-result-title">
        {result.result.output_text === null
          ? <Hint>{ui.t("runDetail.result.noOutput")}</Hint>
          : <>
            <p className="notice">{ui.t(result.result.finish_reason === "refusal"
              ? "runDetail.result.finalAnswerRefusal"
              : "runDetail.result.finalAnswer")}</p>
            {/* Model output is rendered as inert text, never as markup. */}
            <CodeBlock>{result.result.output_text}</CodeBlock>
          </>}
        <Metrics>
          <Metric
            label={ui.t("runDetail.result.modelSteps")}
            value={formatNumber(result.result.model_steps.length, ui.locale)}
          />
          <Metric
            label={ui.t("runDetail.result.inputTokens")}
            value={formatNumber(result.result.recorded_input_tokens, ui.locale)}
          />
          <Metric
            label={ui.t("runDetail.result.outputTokens")}
            value={formatNumber(result.result.recorded_output_tokens, ui.locale)}
          />
          <Metric
            label={ui.t("runDetail.result.toolsSelected")}
            value={formatNumber(result.result.selected_tools.length, ui.locale)}
          />
        </Metrics>
        {result.result.selected_tools.length > 0 && <Hint>
          {ui.t("runDetail.result.selectionNotice", {
            tools: result.result.selected_tools.join(", "),
          })}
        </Hint>}
        {result.result.model_steps.length > 0 && <div className="table-scroll">
          <table className="table">
            <caption>{ui.t("runDetail.steps.caption")}</caption>
            <thead><tr>
              <th scope="col">{ui.t("runDetail.steps.step")}</th>
              <th scope="col">{ui.t("runDetail.steps.model")}</th>
              <th scope="col">{ui.t("runDetail.steps.finish")}</th>
              <th scope="col">{ui.t("runDetail.steps.input")}</th>
              <th scope="col">{ui.t("runDetail.steps.output")}</th>
            </tr></thead>
            <tbody>{result.result.model_steps.map(step => <tr key={step.step_no}>
              <td data-label={ui.t("runDetail.steps.step")}>
                {formatNumber(step.step_no, ui.locale)}</td>
              <td data-label={ui.t("runDetail.steps.model")}>
                <code>{step.provider}/{step.model}</code></td>
              <td data-label={ui.t("runDetail.steps.finish")}><code>{step.finish_reason}</code></td>
              <td data-label={ui.t("runDetail.steps.input")}>
                {formatNumber(step.input_tokens, ui.locale)}</td>
              <td data-label={ui.t("runDetail.steps.output")}>
                {formatNumber(step.output_tokens, ui.locale)}</td>
            </tr>)}</tbody>
          </table>
        </div>}
      </Panel>}

      <SectionHead title={ui.t("runDetail.timelineTitle")}/>
      {events.items.length === 0
        ? <EmptyState title={ui.t("runDetail.timeline.emptyTitle")}>
          <p>{ui.t("runDetail.timeline.emptyBody")}</p>
        </EmptyState>
        : <ol className="timeline">{events.items.map(event => {
          const detail = eventDetail(event.payload);
          const key = runEventKey(event.event_type);
          return <li key={event.id}>
            <p className="timeline-head">
              {/* An event type the console has no name for is still listed, by its
                  recorded identifier, so nothing disappears from the history. */}
              <span>{key ? ui.t(key) : <code>{event.event_type}</code>}</span>
              <time dateTime={event.created_at}>
                {formatTimestamp(event.created_at, ui.locale)}</time>
            </p>
            {key && <p className="field-help"><code>{event.event_type}</code></p>}
            {detail && <p className="notice">{detail}</p>}
          </li>;
        })}</ol>}
      {events.next_cursor && <Hint>{ui.t("runDetail.timeline.limit")}</Hint>}
    </ConsoleShell>;
  } catch (error) {
    return <ConsoleProblem
      chrome={chrome} error={error} area={chrome.ui.t("runDetail.area")} active="runs"
      back={{ href: runsRoot, label: chrome.ui.t("runs.backToRuns") }}
    />;
  }
}
