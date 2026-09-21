import { redirect } from "next/navigation";
import { z } from "zod";
import {
  LIMIT_DECIMALS, MAX_ALERT_THRESHOLDS, MAX_LIMIT_UNITS, categoryKey, microsToUnits,
  offeredThresholds, periodLastDay, spendCategorySchema, spendRecordPageSchema,
  spendSummarySchema, usedRatio, type SpendSummary,
} from "../../../../lib/spend-contracts";
import { api } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { consoleChrome } from "../../../../lib/server/chrome";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import {
  decimalSeparator, formatDate, formatIntegerPercent, formatNumber, formatPercent,
  formatTimestamp, formatUnitsFrom, toLimitInput,
} from "../../../../lib/i18n/format.ts";
import type { Ui } from "../../../../lib/i18n/messages.ts";
import type { Tone } from "../../../../lib/i18n/tone.ts";
import { ConsoleBreadcrumb, ConsoleShell } from "../../../../components/shell/ConsoleShell.tsx";
import {
  EmptyState, Field, FilterLink, Filters, Hint, Metric, Metrics, Notice, PageHeader,
  Pagination, Panel, SectionHead, StatusBadge,
} from "../../../../components/ui/primitives.tsx";
import { Reveal } from "../../../../components/ui/Reveal.tsx";
import { ConsoleProblem, Failed, InvalidLinkPage, Saved } from "../console";
import type { MessageKey } from "../../../../messages/en.ts";

const MESSAGES: Record<string, MessageKey> = {
  invalid: "spend.error.invalid",
  thresholds: "spend.error.thresholds",
  forbidden: "spend.error.forbidden",
  failed: "spend.error.failed",
};

/** Amounts are exact micros; the display groups them without ever touching a float. */
function units(micros: number, ui: Ui): string {
  return formatUnitsFrom(microsToUnits(micros), ui.locale);
}

// Budget state is a status, not a series: it is always written out in words next to the
// colour, so the meter is never the only thing carrying the message.
function budgetState(summary: SpendSummary): { tone: Tone; key: MessageKey } {
  if (summary.monthly_limit_micros === null) return { tone: "warn", key: "spend.state.noBudget" };
  if (summary.exhausted) return { tone: "down", key: "spend.state.exhausted" };
  if (summary.consumed_micros >= summary.monthly_limit_micros) {
    return { tone: "warn", key: "spend.state.overLimitMonitored" };
  }
  return (usedRatio(summary) ?? 0) >= 0.8
    ? { tone: "warn", key: "spend.state.approaching" }
    : { tone: "up", key: "spend.state.withinBudget" };
}

function SpendMeter({ ui, summary }: { ui: Ui; summary: SpendSummary }) {
  const ratio = usedRatio(summary);
  if (ratio === null || summary.monthly_limit_micros === null) return null;
  const state = budgetState(summary);
  return <div>
    <div
      className={"meter-track tone-" + state.tone}
      role="meter"
      aria-valuemin={0}
      aria-valuemax={summary.monthly_limit_micros}
      aria-valuenow={Math.min(summary.consumed_micros, summary.monthly_limit_micros)}
      aria-valuetext={ui.t("spend.meterText", {
        consumed: units(summary.consumed_micros, ui),
        limit: units(summary.monthly_limit_micros, ui),
      })}
    ><span className="meter-fill" style={{ width: (ratio * 100).toFixed(1) + "%" }}/></div>
    <p className="notice">{ui.t("spend.meterCaption", {
      percent: formatPercent(ratio, ui.locale),
    })}</p>
  </div>;
}

export default async function Spend({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{
    cursor?: string; category?: string; saved?: string; budget_error?: string;
  }>;
}) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const category = query.category ? spendCategorySchema.safeParse(query.category) : null;
  const root = `/workspaces/${id}/spend`;
  let chrome = await consoleChrome(session);
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !z.uuid().safeParse(query.cursor).success)
    || (category !== null && !category.success)) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace"),
    }}/>;
  }
  const filter = category?.success ? `&category=${category.data}` : "";

  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
    const summary = await api(session, `/api/v1/workspaces/${id}/spend`, spendSummarySchema);
    const records = await api(
      session,
      `/api/v1/workspaces/${id}/spend/records?limit=25${filter}`
        + (query.cursor ? "&cursor=" + query.cursor : ""),
      spendRecordPageSchema,
    );
    const state = budgetState(summary);
    const calls = summary.categories.reduce((total, row) => total + row.call_count, 0);
    const canManage = workspace.role !== "member";
    const separator = decimalSeparator(ui.locale);
    const period = `${formatDate(summary.period_start, ui.locale)} – `
      + `${new Intl.DateTimeFormat(ui.tag, { dateStyle: "medium", timeZone: "UTC" })
        .format(periodLastDay(summary))} UTC`;

    return <ConsoleShell
      chrome={chrome}
      active="spend"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace} trail={[{ href: root, label: ui.t("navigation.spend") }]}
      />}
    >
      <PageHeader
        eyebrow={ui.t("spend.eyebrow")}
        title={ui.t("spend.title")}
        intro={ui.t("spend.intro", { period })}
      />
      <Hint>{ui.t("spend.accountingNotice")}</Hint>
      <Saved message={query.saved === "budget" ? ui.t("spend.saved") : null}/>
      <Failed message={query.budget_error
        ? ui.t(MESSAGES[query.budget_error] ?? MESSAGES.failed, {
          min: formatNumber(0, ui.locale),
          max: formatNumber(MAX_LIMIT_UNITS, ui.locale),
          decimals: formatNumber(LIMIT_DECIMALS, ui.locale),
        })
        : null}/>

      <Panel label={ui.t("spend.summaryLabel")} testId="spend-summary">
        <StatusBadge tone={state.tone}>{ui.t(state.key)}</StatusBadge>
        <Metrics>
          <Metric label={ui.t("spend.consumed")} value={units(summary.consumed_micros, ui)}/>
          <Metric label={ui.t("spend.monthlyLimit")} value={summary.monthly_limit_micros === null
            ? ui.t("common.empty") : units(summary.monthly_limit_micros, ui)}/>
          <Metric label={ui.t("spend.remaining")} value={summary.remaining_micros === null
            ? ui.t("common.empty") : units(summary.remaining_micros, ui)}/>
          <Metric label={ui.t("spend.pricedCalls")} value={formatNumber(calls, ui.locale)}/>
        </Metrics>
        <SpendMeter ui={ui} summary={summary}/>
        {/* The rounded summary above and the exact recorded total are both shown, so a
            display rounding can never be mistaken for the ledger. */}
        <Hint>{ui.t("spend.exactTotal", {
          micros: ui.t("format.microsUnit", {
            value: formatNumber(summary.consumed_micros, ui.locale),
          }),
        })}</Hint>
        {summary.exhausted
          && <Notice live="alert" tone="danger">{ui.t("spend.exhaustedNotice")}</Notice>}
        {summary.enforcement === "monitor" && <Hint>{ui.t("spend.monitorNotice")}</Hint>}
        {summary.monthly_limit_micros === null && <Hint>{ui.t("spend.noBudgetNotice")}</Hint>}
        {summary.alerts.length > 0 && <div className="stack" data-testid="spend-alerts">
          <h3 className="section-title">{ui.t("spend.alertsTitle")}</h3>
          <ul className="notice" style={{ paddingInlineStart: "20px" }}>
            {summary.alerts.map(alert => <li key={alert.threshold_percent}>
              {ui.t("spend.alertEntry", {
                percent: formatIntegerPercent(alert.threshold_percent, ui.locale),
                consumed: units(alert.consumed_micros, ui),
                limit: units(alert.monthly_limit_micros, ui),
              })}
              {" · "}
              <time dateTime={alert.created_at}>
                {formatTimestamp(alert.created_at, ui.locale)}</time>
            </li>)}
          </ul>
          <Hint>{ui.t("spend.alertsNotice")}</Hint>
        </div>}
        {summary.monthly_limit_micros !== null && summary.alert_thresholds.length === 0
          && <Hint>{ui.t("spend.noThresholdsNotice")}</Hint>}
      </Panel>

      <SectionHead title={ui.t("spend.categoriesTitle")}/>
      {summary.categories.length === 0
        ? <EmptyState title={ui.t("spend.categoriesEmptyTitle")}>
          <p>{ui.t("spend.categoriesEmptyBody")}</p>
        </EmptyState>
        : <div className="table-scroll"><table className="table" data-testid="spend-categories">
          <caption>{ui.t("spend.categoriesCaption")}</caption>
          <thead><tr>
            <th scope="col">{ui.t("spend.column.category")}</th>
            <th scope="col">{ui.t("spend.column.calls")}</th>
            <th scope="col">{ui.t("spend.column.inputTokens")}</th>
            <th scope="col">{ui.t("spend.column.outputTokens")}</th>
            <th scope="col">{ui.t("spend.column.cost")}</th>
          </tr></thead>
          <tbody>{summary.categories.map(row => <tr key={row.category}>
            <th scope="row" data-label={ui.t("spend.column.category")}>
              {ui.t(categoryKey(row.category))}</th>
            <td data-label={ui.t("spend.column.calls")}>
              {formatNumber(row.call_count, ui.locale)}</td>
            <td data-label={ui.t("spend.column.inputTokens")}>
              {formatNumber(row.input_tokens, ui.locale)}</td>
            <td data-label={ui.t("spend.column.outputTokens")}>
              {formatNumber(row.output_tokens, ui.locale)}</td>
            <td data-label={ui.t("spend.column.cost")}>{units(row.cost_micros, ui)}</td>
          </tr>)}</tbody>
        </table></div>}

      <Reveal>
        <Panel labelledBy="budget-title">
          <h2 id="budget-title" className="section-title">{ui.t("spend.budgetTitle")}</h2>
          {canManage
            ? <form className="form" action="/workspaces/spend/budget" method="post">
              <Hint>{ui.t("spend.budgetNotice")}</Hint>
              <input type="hidden" name="csrf" value={session.csrf}/>
              <input type="hidden" name="workspace" value={id}/>
              <Field
                id="limit"
                label={ui.t("spend.limitLabel")}
                help={ui.t("spend.limitHelp", {
                  decimals: formatNumber(LIMIT_DECIMALS, ui.locale), separator,
                })}
              >
                <input
                  className="field-control" id="limit" name="limit" required inputMode="decimal"
                  autoComplete="off" pattern="\d{1,10}([.,]\d{1,6})?"
                  defaultValue={toLimitInput(
                    microsToUnits(summary.monthly_limit_micros ?? 0), ui.locale,
                  )}
                  aria-describedby="limit-help"
                />
              </Field>
              <fieldset className="bordered">
                <legend>{ui.t("spend.thresholdsLegend")}</legend>
                <Hint>{ui.t("spend.thresholdsNotice")}</Hint>
                <div className="choice-list">
                  {offeredThresholds(summary.alert_thresholds).map(percent => <label
                    key={percent} className="choice" htmlFor={`threshold-${percent}`}
                  >
                    <input
                      type="checkbox" id={`threshold-${percent}`} name="threshold"
                      value={percent} defaultChecked={summary.alert_thresholds.includes(percent)}
                    />
                    {ui.t("spend.thresholdOption", {
                      percent: formatIntegerPercent(percent, ui.locale),
                    })}
                  </label>)}
                </div>
              </fieldset>
              <Field id="enforcement" label={ui.t("spend.enforcementLabel")}>
                <select
                  className="field-control" id="enforcement" name="enforcement"
                  defaultValue={summary.enforcement ?? "enforce"}
                >
                  <option value="enforce">{ui.t("spend.enforce")}</option>
                  <option value="monitor">{ui.t("spend.monitor")}</option>
                </select>
              </Field>
              <div><button type="submit" className="button">{ui.t("spend.saveBudget")}</button></div>
            </form>
            : <Hint>{ui.t("spend.readOnlyNotice")}</Hint>}
        </Panel>
      </Reveal>

      <SectionHead title={ui.t("spend.ledgerTitle")}/>
      <Filters label={ui.t("spend.ledgerFilterLabel")}>
        <FilterLink href={root} current={category === null}>{ui.t("common.all")}</FilterLink>
        {spendCategorySchema.options.map(option => <FilterLink
          key={option}
          href={`${root}?category=${option}`}
          current={Boolean(category?.success && category.data === option)}
        >{ui.t(categoryKey(option))}</FilterLink>)}
      </Filters>
      {records.items.length === 0
        ? <EmptyState title={ui.t("spend.ledgerEmptyTitle")}>
          <p>{ui.t("spend.ledgerEmptyBody")}</p>
        </EmptyState>
        : <div className="table-scroll"><table className="table" data-testid="spend-ledger">
          <caption>{ui.t("spend.ledgerCaption")}</caption>
          <thead><tr>
            <th scope="col">{ui.t("spend.column.recorded")}</th>
            <th scope="col">{ui.t("spend.column.category")}</th>
            <th scope="col">{ui.t("spend.column.model")}</th>
            <th scope="col">{ui.t("spend.column.tokens")}</th>
            <th scope="col">{ui.t("spend.column.cost")}</th>
            <th scope="col">{ui.t("spend.column.source")}</th>
          </tr></thead>
          <tbody>{records.items.map(record => <tr key={record.id}>
            <td data-label={ui.t("spend.column.recorded")}>
              <time dateTime={record.occurred_at}>
                {formatTimestamp(record.occurred_at, ui.locale)}</time></td>
            <td data-label={ui.t("spend.column.category")}>{ui.t(categoryKey(record.category))}</td>
            <td data-label={ui.t("spend.column.model")}>
              <code>{record.provider}/{record.model}</code></td>
            <td data-label={ui.t("spend.column.tokens")}>
              {formatNumber(record.input_tokens + record.output_tokens, ui.locale)}</td>
            <td data-label={ui.t("spend.column.cost")}>{units(record.cost_micros, ui)}</td>
            <td data-label={ui.t("spend.column.source")}><code>{record.source_key}</code></td>
          </tr>)}</tbody>
        </table></div>}

      <Pagination
        label={ui.t("spend.pagesLabel")}
        previous={query.cursor
          ? {
            href: root + (filter ? "?" + filter.slice(1) : ""), label: ui.t("spend.newestCalls"),
          }
          : null}
        next={records.next_cursor
          ? {
            href: `${root}?cursor=${records.next_cursor}${filter}`,
            label: ui.t("spend.olderCalls"),
          }
          : null}
      />
    </ConsoleShell>;
  } catch (error) {
    return <ConsoleProblem
      chrome={chrome} error={error} area={chrome.ui.t("spend.area")} active="spend"
      back={{ href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace") }}
    />;
  }
}
