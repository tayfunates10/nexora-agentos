import { redirect } from "next/navigation";
import { ApiError } from "../../../../lib/server/api";
import type { EvalRunSummary } from "../../../../lib/evaluation-contracts";
import type { Ui } from "../../../../lib/i18n/messages.ts";
import { formatNumber, formatTimestamp } from "../../../../lib/i18n/format.ts";
import { ConsoleShell, type ConsoleChrome } from "../../../../components/shell/ConsoleShell.tsx";
import { Metric, Metrics, Notice } from "../../../../components/ui/primitives.tsx";

export function EvaluationProblem({ chrome, error, back }: {
  chrome: ConsoleChrome; error: unknown; back: { href: string; label: string };
}) {
  if (error instanceof ApiError && error.status === 401) redirect("/login?error=session_expired");
  const { ui } = chrome;
  const denied = error instanceof ApiError && [403, 404].includes(error.status);
  return <ConsoleShell chrome={chrome} active="evaluations">
    <h1 className="page-title">
      {ui.t(denied ? "evaluations.deniedTitle" : "evaluations.unavailableTitle")}</h1>
    <Notice live="alert" tone="danger">
      {ui.t(denied ? "evaluations.deniedBody" : "evaluations.unavailableBody")}</Notice>
    <p><a className="link" href={back.href}>{back.label}</a></p>
  </ConsoleShell>;
}

/** The same failure, reported inside a page that otherwise loaded. */
export function EvaluationInlineProblem({ ui, error }: { ui: Ui; error: unknown }) {
  if (error instanceof ApiError && error.status === 401) redirect("/login?error=session_expired");
  const denied = error instanceof ApiError && [403, 404].includes(error.status);
  return <Notice live="alert" tone="danger">
    {ui.t(denied ? "evaluations.deniedBody" : "evaluations.unavailableBody")}
  </Notice>;
}

/**
 * A regression or improvement only means something against a baseline. Without one both
 * counts read as not applicable rather than as zero.
 */
export function RunCounts({ ui, run }: { ui: Ui; run: EvalRunSummary }) {
  return <Metrics>
    <Metric label={ui.t("evaluations.passed")} value={ui.t("evaluations.passedOfTotal", {
      passed: formatNumber(run.passed_count, ui.locale),
      total: formatNumber(run.case_count, ui.locale),
    })}/>
    <Metric label={ui.t("evaluations.failed")} value={formatNumber(run.failed_count, ui.locale)}/>
    <Metric label={ui.t("evaluations.regressions")} value={run.baseline_eval_run_id
      ? formatNumber(run.regression_count, ui.locale) : ui.t("common.empty")}/>
    <Metric label={ui.t("evaluations.improvements")} value={run.baseline_eval_run_id
      ? formatNumber(run.improvement_count, ui.locale) : ui.t("common.empty")}/>
  </Metrics>;
}

export function RunCard({ ui, run, workspaceId }: {
  ui: Ui; run: EvalRunSummary; workspaceId: string;
}) {
  return <article className="card">
    <h3 className="section-title">
      <a className="link" href={`/workspaces/${workspaceId}/evaluations/runs/${run.id}`}>
        {run.candidate_label}</a>
    </h3>
    <p className="field-help">
      <time dateTime={run.created_at}>{formatTimestamp(run.created_at, ui.locale)}</time></p>
    <RunCounts ui={ui} run={run}/>
    <p className="notice">{ui.t(run.baseline_eval_run_id
      ? "evaluations.baselineCompared" : "evaluations.baselineNone")}</p>
  </article>;
}
