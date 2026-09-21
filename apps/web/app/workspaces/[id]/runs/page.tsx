import { redirect } from "next/navigation";
import { z } from "zod";
import {
  runDurationSeconds, runPageSchema, runStatusKey, runStatusSchema,
} from "../../../../lib/agent-contracts";
import { api } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { consoleChrome } from "../../../../lib/server/chrome";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { formatDuration, formatNumber, formatTimestamp } from "../../../../lib/i18n/format.ts";
import { ConsoleBreadcrumb, ConsoleShell } from "../../../../components/shell/ConsoleShell.tsx";
import {
  EmptyState, FilterLink, Filters, Hint, PageHeader, Pagination, RefreshLink, SectionHead,
} from "../../../../components/ui/primitives.tsx";
import { TextWithLink } from "../../../../components/ui/RichText.tsx";
import { ConsoleProblem, InvalidLinkPage, RunStatusBadge } from "../console";

export default async function Runs({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string; status?: string; mine?: string }>;
}) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const status = query.status ? runStatusSchema.safeParse(query.status) : null;
  const mine = query.mine === "1";
  const root = `/workspaces/${id}/runs`;
  let chrome = await consoleChrome(session);
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !z.uuid().safeParse(query.cursor).success)
    || (status !== null && !status.success)
    || (query.mine !== undefined && query.mine !== "1")) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace"),
    }}/>;
  }

  // Filters live in the URL, so the language switch, a refresh and the back button all
  // return to exactly the same view.
  const filters = (status?.success ? `&status=${status.data}` : "") + (mine ? "&mine=1" : "");
  const link = (extra: string) => root + (filters || extra ? "?" + (filters + extra).slice(1) : "");

  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
    const runs = await api(
      session,
      `/api/v1/workspaces/${id}/runs?limit=25`
        + (status?.success ? `&status=${status.data}` : "")
        + (mine ? "&requested_by_me=true" : "")
        + (query.cursor ? "&cursor=" + query.cursor : ""),
      runPageSchema,
    );

    return <ConsoleShell
      chrome={chrome}
      active="runs"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace} trail={[{ href: root, label: ui.t("navigation.runs") }]}
      />}
    >
      <PageHeader
        eyebrow={ui.t("runs.eyebrow")}
        title={ui.t("runs.title")}
        intro={ui.t("runs.intro")}
      />
      <SectionHead
        title={ui.t("runs.filterLabel")}
        action={<RefreshLink href={link("")} label={ui.t("common.refresh")}/>}
      />
      <Hint>{ui.t("runs.updateNotice")} {ui.t("common.utcNote")}</Hint>

      <Filters label={ui.t("runs.filterLabel")}>
        <FilterLink href={root + (mine ? "?mine=1" : "")} current={status === null}>
          {ui.t("runs.allStatuses")}
        </FilterLink>
        {runStatusSchema.options.map(option => <FilterLink
          key={option}
          href={`${root}?status=${option}` + (mine ? "&mine=1" : "")}
          current={Boolean(status?.success && status.data === option)}
        >{ui.t(runStatusKey(option))}</FilterLink>)}
        <FilterLink
          href={mine
            ? root + (status?.success ? `?status=${status.data}` : "")
            : `${root}?mine=1` + (status?.success ? `&status=${status.data}` : "")}
          current={mine}
        >{ui.t(mine ? "runs.startedByMeActive" : "runs.startedByMe")}</FilterLink>
      </Filters>

      {runs.items.length === 0
        ? <EmptyState title={ui.t("runs.emptyTitle")}>
          <p><TextWithLink
            ui={ui} message="runs.emptyBody" link="runs.emptyBodyLink"
            href={`/workspaces/${id}/agents`}
          /></p>
        </EmptyState>
        : <div className="table-scroll"><table className="table">
          <caption>{ui.t("runs.tableCaption")}</caption>
          <thead><tr>
            <th scope="col">{ui.t("runs.column.started")}</th>
            <th scope="col">{ui.t("runs.column.agent")}</th>
            <th scope="col">{ui.t("runs.column.status")}</th>
            <th scope="col">{ui.t("runs.column.attempts")}</th>
            <th scope="col">{ui.t("runs.column.duration")}</th>
            <th scope="col">{ui.t("runs.column.run")}</th>
          </tr></thead>
          <tbody>{runs.items.map(run => {
            const seconds = runDurationSeconds(run);
            return <tr key={run.id}>
              <td data-label={ui.t("runs.column.started")}>
                <time dateTime={run.created_at}>{formatTimestamp(run.created_at, ui.locale)}</time>
              </td>
              <td data-label={ui.t("runs.column.agent")}>{run.agent_name}</td>
              <td data-label={ui.t("runs.column.status")}>
                <RunStatusBadge chrome={chrome} status={run.status}/>
                {/* A failure code is an API identifier, shown exactly as recorded. */}
                {run.failure_code && <p className="field-help"><code>{run.failure_code}</code></p>}
              </td>
              <td data-label={ui.t("runs.column.attempts")}>
                {formatNumber(run.attempt_count, ui.locale)}</td>
              <td data-label={ui.t("runs.column.duration")}>
                {seconds === null ? ui.t("common.empty") : formatDuration(seconds, ui.t)}</td>
              <td data-label={ui.t("runs.column.run")}>
                <a className="link" href={`${root}/${run.id}`}>
                  {ui.t(run.requested_by_me ? "runs.openRunMine" : "runs.openRun")}</a></td>
            </tr>;
          })}</tbody>
        </table></div>}

      <Pagination
        label={ui.t("runs.pagesLabel")}
        previous={query.cursor ? { href: link(""), label: ui.t("runs.newest") } : null}
        next={runs.next_cursor
          ? { href: link(`&cursor=${runs.next_cursor}`), label: ui.t("runs.older") }
          : null}
      />
    </ConsoleShell>;
  } catch (error) {
    return <ConsoleProblem
      chrome={chrome} error={error} area={chrome.ui.t("runs.area")} active="runs"
      back={{ href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace") }}
    />;
  }
}
