import { redirect } from "next/navigation";
import { z } from "zod";
import { currentSession } from "../../../../../../lib/server/session";
import { api } from "../../../../../../lib/server/api";
import { consoleChrome } from "../../../../../../lib/server/chrome";
import { workspaceSchema } from "../../../../../../lib/workspace-contracts";
import { evalRunPageSchema, evalSuiteSchema } from "../../../../../../lib/evaluation-contracts";
import { formatNumber } from "../../../../../../lib/i18n/format.ts";
import {
  ConsoleBreadcrumb, ConsoleShell,
} from "../../../../../../components/shell/ConsoleShell.tsx";
import {
  Badge, CodeBlock, Detail, DetailList, Disclosure, EmptyState, Hint, PageHeader,
  Pagination, SectionHead,
} from "../../../../../../components/ui/primitives.tsx";
import { EvaluationInlineProblem, EvaluationProblem, RunCard } from "../../shared";
import { InvalidLinkPage } from "../../../console";

export default async function SuiteDetail({ params, searchParams }: {
  params: Promise<{ id: string; suiteId: string }>; searchParams: Promise<{ cursor?: string }>;
}) {
  const [{ id, suiteId }, { cursor }] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const root = `/workspaces/${id}/evaluations`;
  const suiteRoot = `${root}/suites/${suiteId}`;
  let chrome = await consoleChrome(session);
  if (![id, suiteId].every(value => z.uuid().safeParse(value).success)
    || (cursor !== undefined && !z.uuid().safeParse(cursor).success)) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: "/workspaces", label: chrome.ui.t("workspaces.backToWorkspaces"),
    }}/>;
  }

  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
    const suite = await api(
      session, `/api/v1/workspaces/${id}/eval-suites/${suiteId}`, evalSuiteSchema,
    );
    // Evaluation results are owner and admin material. A member reads the suite itself and
    // is told plainly why the history is not shown, rather than seeing an empty list.
    let history;
    let historyError;
    if (workspace.role !== "member") {
      try {
        history = await api(
          session,
          `/api/v1/workspaces/${id}/eval-suites/${suiteId}/runs?limit=25`
            + (cursor ? "&cursor=" + cursor : ""),
          evalRunPageSchema,
        );
      } catch (error) { historyError = error; }
    }

    return <ConsoleShell
      chrome={chrome}
      active="evaluations"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace} trail={[
          { href: root, label: ui.t("navigation.evaluations") },
          { href: suiteRoot, label: suite.name },
        ]}
      />}
    >
      <PageHeader
        eyebrow={ui.t("evaluations.suiteMeta", {
          version: formatNumber(suite.version, ui.locale), count: suite.case_count,
        })}
        title={suite.name}
        intro={suite.description ?? undefined}
      />

      <SectionHead title={ui.t("evaluations.casesTitle")}/>
      <div className="card-list">{suite.cases.map(item => <article className="card" key={item.id}>
        <Disclosure summary={<>
          <Badge>{formatNumber(item.case_no, ui.locale)}</Badge>
          <code>{item.case_key}</code>
        </>}>
          {/* Case input and expectations are stored test data, shown exactly as written. */}
          <CodeBlock>{item.input}</CodeBlock>
          <DetailList>
            <Detail label={ui.t("evaluations.expectedTools")}>
              {item.expected_tools.join(", ") || ui.t("evaluations.noneRequired")}</Detail>
            <Detail label={ui.t("evaluations.forbiddenTools")}>
              {item.forbidden_tools.join(", ") || ui.t("evaluations.noneSpecified")}</Detail>
            <Detail label={ui.t("evaluations.requiredCitations")}>
              {item.expected_citations.join(", ") || ui.t("evaluations.noneRequired")}</Detail>
          </DetailList>
        </Disclosure>
      </article>)}</div>

      <SectionHead title={ui.t("evaluations.historyTitle")}/>
      {workspace.role === "member" && <Hint>{ui.t("evaluations.historyMemberNotice")}</Hint>}
      {historyError !== undefined && <EvaluationInlineProblem ui={ui} error={historyError}/>}
      {history && <>
        <Hint>{ui.t("evaluations.historyNotice")}</Hint>
        {history.items.length === 0
          ? <EmptyState title={ui.t("evaluations.historyEmptyTitle")}>
            <p>{ui.t("evaluations.historyEmptyBody")}</p>
          </EmptyState>
          : <div className="card-list">{history.items.map(run => <RunCard
            key={run.id} ui={ui} run={run} workspaceId={id}
          />)}</div>}
        <Pagination
          label={ui.t("evaluations.historyPagesLabel")}
          previous={cursor
            ? { href: suiteRoot, label: ui.t("evaluations.newestResults") } : null}
          next={history.next_cursor
            ? {
              href: `${suiteRoot}?cursor=${history.next_cursor}`,
              label: ui.t("evaluations.olderResults"),
            }
            : null}
        />
      </>}
    </ConsoleShell>;
  } catch (error) {
    return <EvaluationProblem chrome={chrome} error={error} back={{
      href: root, label: chrome.ui.t("evaluations.backToSuites"),
    }}/>;
  }
}
