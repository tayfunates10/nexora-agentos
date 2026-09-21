import { randomUUID } from "node:crypto";
import { redirect } from "next/navigation";
import { z } from "zod";
import {
  evalJudgeRunSchema, evalRunSchema, type EvalJudgeRun,
} from "../../../../../../lib/evaluation-contracts";
import { api, ApiError } from "../../../../../../lib/server/api";
import { currentSession } from "../../../../../../lib/server/session";
import { consoleChrome } from "../../../../../../lib/server/chrome";
import { workspaceSchema } from "../../../../../../lib/workspace-contracts";
import {
  formatMilliseconds, formatNumber, formatQuality, formatQualityDelta, formatTimestamp,
} from "../../../../../../lib/i18n/format.ts";
import type { Ui } from "../../../../../../lib/i18n/messages.ts";
import type { Tone } from "../../../../../../lib/i18n/tone.ts";
import {
  ConsoleBreadcrumb, ConsoleShell,
} from "../../../../../../components/shell/ConsoleShell.tsx";
import {
  CodeBlock, Detail, DetailList, Disclosure, Hint, Metric, Metrics, Notice, PageHeader,
  Panel, SectionHead, StatusBadge,
} from "../../../../../../components/ui/primitives.tsx";
import { EvaluationProblem, RunCounts } from "../../shared";
import { InvalidLinkPage } from "../../../console";
import type { MessageKey } from "../../../../../../messages/en.ts";

const JUDGE_TONES: Record<EvalJudgeRun["status"], Tone> = {
  queued: "neutral", running: "warn", succeeded: "up", failed: "down",
};

function JudgeSummary({ ui, judge }: { ui: Ui; judge: EvalJudgeRun }) {
  return <>
    <StatusBadge tone={JUDGE_TONES[judge.status]}>
      {ui.t(`judge.status.${judge.status}` as MessageKey)}
    </StatusBadge>
    <Metrics>
      <Metric label={ui.t("judge.quality")} value={formatQuality(judge.quality_milli, ui.locale, ui.t)}/>
      <Metric label={ui.t("judge.scored")} value={ui.t("evaluations.passedOfTotal", {
        passed: formatNumber(judge.scored_count, ui.locale),
        total: formatNumber(judge.case_count, ui.locale),
      })}/>
      <Metric
        label={ui.t("judge.baseline")}
        value={formatQuality(judge.baseline_quality_milli, ui.locale, ui.t)}
      />
      <Metric
        label={ui.t("judge.qualityDelta")}
        value={formatQualityDelta(judge.quality_delta_milli, ui.locale, ui.t)}
      />
    </Metrics>
    {/* Provider, model and prompt version are pinned identifiers, never translated. */}
    {judge.judge_model && <Hint>{ui.t("judge.pinned", {
      provider: judge.judge_provider ?? "", model: judge.judge_model,
      promptVersion: judge.prompt_version ?? "",
    })}</Hint>}
    {judge.status === "succeeded" && <Hint>{ui.t("judge.totals", {
      regressions: formatNumber(judge.regression_count, ui.locale),
      improvements: formatNumber(judge.improvement_count, ui.locale),
      tokens: formatNumber(judge.input_tokens + judge.output_tokens, ui.locale),
      latency: formatMilliseconds(judge.latency_ms, ui.locale, ui.t),
    })}</Hint>}
  </>;
}

export default async function EvaluationRunDetail({ params, searchParams }: {
  params: Promise<{ id: string; runId: string }>;
  searchParams: Promise<{ judge?: string; judge_error?: string }>;
}) {
  const [{ id, runId }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const root = `/workspaces/${id}/evaluations`;
  let chrome = await consoleChrome(session);
  if (![id, runId].every(value => z.uuid().safeParse(value).success)) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: "/workspaces", label: chrome.ui.t("workspaces.backToWorkspaces"),
    }}/>;
  }

  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
    const run = await api(session, `/api/v1/workspaces/${id}/eval-runs/${runId}`, evalRunSchema);

    let judge: EvalJudgeRun | null = null;
    let judgeUnavailable = false;
    try {
      judge = await api(
        session,
        `/api/v1/workspaces/${id}/eval-runs/${runId}/judge-runs/latest`,
        evalJudgeRunSchema,
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) throw error;
      // A missing judge run is "none yet"; anything else is a judge service that is down,
      // which never calls the deterministic result into question.
      if (!(error instanceof ApiError && error.status === 404)) judgeUnavailable = true;
    }

    const judgeEligible = run.results.every(result => result.source_agent_run_id !== null);
    const judgeScores = new Map((judge?.results ?? []).map(result => [result.case_id, result]));
    const canQueue = !judgeUnavailable && judgeEligible
      && (judge === null || judge.status === "failed");

    return <ConsoleShell
      chrome={chrome}
      active="evaluations"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace} trail={[
          { href: root, label: ui.t("navigation.evaluations") },
          { href: `${root}/suites/${run.suite_id}`, label: ui.t("evaluations.backToSuiteHistory") },
        ]}
      />}
    >
      <PageHeader eyebrow={ui.t("evaluations.resultEyebrow")} title={run.candidate_label}/>
      <p className="field-help">
        <time dateTime={run.created_at}>{formatTimestamp(run.created_at, ui.locale)}</time></p>
      <RunCounts ui={ui} run={run}/>
      {run.baseline_eval_run_id
        ? <p><a className="link" href={`${root}/runs/${run.baseline_eval_run_id}`}>
          {ui.t("evaluations.viewBaseline")}</a></p>
        : <Hint>{ui.t("evaluations.noBaselineSelected")}</Hint>}

      <Panel labelledBy="quality-judge-heading" testId="judge-panel">
        <p className="eyebrow">{ui.t("judge.eyebrow")}</p>
        <h2 id="quality-judge-heading" className="section-title">{ui.t("judge.title")}</h2>
        <Hint>{ui.t("judge.intro")}</Hint>
        {query.judge === "queued" && <Notice live="status" tone="success">{ui.t("judge.queued")}</Notice>}
        {query.judge_error === "unavailable"
          && <Notice live="alert" tone="danger">{ui.t("judge.error.unavailable")}</Notice>}
        {query.judge_error === "failed"
          && <Notice live="alert" tone="danger">{ui.t("judge.error.failed")}</Notice>}
        {judgeUnavailable
          && <Notice live="alert" tone="warning">{ui.t("judge.statusUnavailable")}</Notice>}
        {judge && <JudgeSummary ui={ui} judge={judge}/>}
        {judge?.status === "failed" && <Notice live="alert" tone="danger">
          {ui.t("judge.failedWithCode", { code: judge.error_code ?? ui.t("judge.unknownCode") })}
        </Notice>}
        {judge && ["queued", "running"].includes(judge.status) && <p>
          <a className="link" href={`${root}/runs/${runId}`}>{ui.t("judge.refreshStatus")}</a></p>}
        {!judgeEligible && judge === null && <Hint>{ui.t("judge.ineligible")}</Hint>}
        {canQueue && <form action="/workspaces/evaluations/judge" method="post">
          <input type="hidden" name="csrf" value={session.csrf}/>
          <input type="hidden" name="workspace" value={id}/>
          <input type="hidden" name="eval_run" value={runId}/>
          <input type="hidden" name="idempotency_key" value={randomUUID()}/>
          <button type="submit" className="button">
            {ui.t(judge?.status === "failed" ? "judge.retry" : "judge.run")}</button>
        </form>}
      </Panel>

      <SectionHead title={ui.t("evaluations.caseResultsTitle")}/>
      <div className="card-list">{run.results.map(result => {
        const score = judgeScores.get(result.case_id);
        return <article className="card" key={result.case_id}>
          <StatusBadge tone={result.passed ? "up" : "down"}>
            {ui.t(result.passed ? "evaluations.casePassed" : "evaluations.caseFailed")}
            {result.regression ? ` · ${ui.t("evaluations.caseRegression")}`
              : result.improvement ? ` · ${ui.t("evaluations.caseImprovement")}` : ""}
          </StatusBadge>
          <h3 className="section-title"><code>{result.case_key}</code></h3>
          {result.baseline_passed !== null && <p className="notice">
            {ui.t(result.baseline_passed
              ? "evaluations.baselinePassed" : "evaluations.baselineFailed")}</p>}
          {/* Failure lines come from the evaluator as recorded evidence, not as copy. */}
          {result.failures.length > 0 && <ul className="notice">
            {result.failures.map((failure, index) => <li key={index}>{failure}</li>)}</ul>}
          <DetailList>
            <Detail label={ui.t("evaluations.selectedTools")}>
              {result.selected_tools.join(", ") || ui.t("common.none")}</Detail>
            <Detail label={ui.t("evaluations.citations")}>
              {result.citations.join(", ") || ui.t("common.none")}</Detail>
          </DetailList>
          {score && <div className="stack">
            <StatusBadge tone={score.regression ? "down" : score.improvement ? "up" : "neutral"}>
              {ui.t("judge.caseLabel", {
                quality: formatQuality(score.quality_milli, ui.locale, ui.t),
              })}
              {score.regression ? ` · ${ui.t("judge.caseRegression")}`
                : score.improvement ? ` · ${ui.t("judge.caseImprovement")}` : ""}
            </StatusBadge>
            <DetailList>
              <Detail label={ui.t("judge.taskCompletion")}>{ui.t("judge.scoreOfFour", {
                score: formatNumber(score.task_completion, ui.locale) })}</Detail>
              <Detail label={ui.t("judge.relevance")}>{ui.t("judge.scoreOfFour", {
                score: formatNumber(score.answer_relevance, ui.locale) })}</Detail>
              <Detail label={ui.t("judge.clarity")}>{ui.t("judge.scoreOfFour", {
                score: formatNumber(score.clarity, ui.locale) })}</Detail>
              {score.baseline_quality_milli !== null && <>
                <Detail label={ui.t("judge.baseline")}>
                  {formatQuality(score.baseline_quality_milli, ui.locale, ui.t)}</Detail>
                <Detail label={ui.t("judge.qualityDelta")}>
                  {formatQualityDelta(score.quality_delta_milli, ui.locale, ui.t)}</Detail>
              </>}
              {/* The judge's rationale is model output, shown in the language it was
                  produced in and never re-translated. */}
              <Detail label={ui.t("judge.rationale")}>{score.rationale}</Detail>
            </DetailList>
          </div>}
          {!result.passed && (result.raw_output !== null
            ? <Disclosure summary={ui.t("evaluations.failedOutput")}>
              <p className="field-help">{ui.t("evaluations.originalRecord")}</p>
              <CodeBlock>{result.raw_output}</CodeBlock>
            </Disclosure>
            : <Hint>{ui.t("evaluations.noRawOutput")}</Hint>)}
        </article>;
      })}</div>
    </ConsoleShell>;
  } catch (error) {
    return <EvaluationProblem chrome={chrome} error={error} back={{
      href: root, label: chrome.ui.t("evaluations.backToEvaluations"),
    }}/>;
  }
}
