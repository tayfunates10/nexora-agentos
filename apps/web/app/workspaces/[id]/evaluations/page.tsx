import { redirect } from "next/navigation";
import { z } from "zod";
import { currentSession } from "../../../../lib/server/session";
import { api } from "../../../../lib/server/api";
import { consoleChrome } from "../../../../lib/server/chrome";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { evalSuitePageSchema } from "../../../../lib/evaluation-contracts";
import { formatNumber, formatTimestamp } from "../../../../lib/i18n/format.ts";
import { ConsoleBreadcrumb, ConsoleShell } from "../../../../components/shell/ConsoleShell.tsx";
import {
  Badge, EmptyState, Hint, PageHeader, Pagination,
} from "../../../../components/ui/primitives.tsx";
import { EvaluationProblem } from "./shared";
import { InvalidLinkPage } from "../console";

export default async function Evaluations({ params, searchParams }: {
  params: Promise<{ id: string }>; searchParams: Promise<{ cursor?: string }>;
}) {
  const [{ id }, { cursor }] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const root = `/workspaces/${id}/evaluations`;
  let chrome = await consoleChrome(session);
  if (!z.uuid().safeParse(id).success
    || (cursor !== undefined && !z.uuid().safeParse(cursor).success)) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: "/workspaces", label: chrome.ui.t("workspaces.backToWorkspaces"),
    }}/>;
  }

  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
    const suites = await api(
      session,
      `/api/v1/workspaces/${id}/eval-suites?limit=25${cursor ? "&cursor=" + cursor : ""}`,
      evalSuitePageSchema,
    );

    return <ConsoleShell
      chrome={chrome}
      active="evaluations"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace}
        trail={[{ href: root, label: ui.t("navigation.evaluations") }]}
      />}
    >
      <PageHeader
        eyebrow={ui.t("evaluations.eyebrow")}
        title={ui.t("evaluations.title")}
        intro={ui.t("evaluations.intro")}
      />
      <Hint>{ui.t("evaluations.deterministicNotice")}</Hint>

      {suites.items.length === 0
        ? <EmptyState title={ui.t("evaluations.emptyTitle")}>
          <p>{ui.t("evaluations.emptyBody")}</p>
        </EmptyState>
        : <div className="card-list">{suites.items.map(suite => <article
          className="card" key={suite.id}
        >
          <Badge>{ui.t("evaluations.suiteMeta", {
            version: formatNumber(suite.version, ui.locale), count: suite.case_count,
          })}</Badge>
          {/* Suite name and description are operator content, shown as stored. */}
          <h2 className="section-title">
            <a className="link" href={`${root}/suites/${suite.id}`}>{suite.name}</a></h2>
          {suite.description && <p className="notice">{suite.description}</p>}
          <p className="field-help">
            <time dateTime={suite.created_at}>
              {formatTimestamp(suite.created_at, ui.locale)}</time></p>
        </article>)}</div>}

      <Pagination
        label={ui.t("evaluations.pagesLabel")}
        previous={cursor ? { href: root, label: ui.t("common.firstPage") } : null}
        next={suites.next_cursor
          ? {
            href: `${root}?cursor=${suites.next_cursor}`, label: ui.t("evaluations.nextSuites"),
          }
          : null}
      />
    </ConsoleShell>;
  } catch (error) {
    return <EvaluationProblem chrome={chrome} error={error} back={{
      href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace"),
    }}/>;
  }
}
