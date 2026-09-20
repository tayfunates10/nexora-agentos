import { redirect } from "next/navigation";
import { z } from "zod";
import {
  CATEGORY_LABELS,
  formatMicros,
  formatUnits,
  microsToUnits,
  spendCategorySchema,
  spendPeriod,
  spendRecordPageSchema,
  spendSummarySchema,
  spendTimestamp,
  usedRatio,
  type SpendSummary,
} from "../../../../lib/spend-contracts";
import { api, ApiError } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { workspaceSchema } from "../../../../lib/workspace-contracts";

// Budget state is a status, not a series: it is always written out in words next to
// the colour so the meter is never the only thing carrying the message.
function budgetState(summary: SpendSummary): { tone: string; label: string } {
  if (summary.monthly_limit_micros === null) return { tone: "unknown", label: "No budget set" };
  if (summary.exhausted) return { tone: "down", label: "Budget exhausted" };
  if (summary.consumed_micros >= summary.monthly_limit_micros) {
    return { tone: "unknown", label: "Over limit · monitored, not blocked" };
  }
  const ratio = usedRatio(summary) ?? 0;
  return ratio >= 0.8
    ? { tone: "unknown", label: "Approaching the monthly limit" }
    : { tone: "up", label: "Within budget" };
}

function SpendMeter({ summary }: { summary: SpendSummary }) {
  const ratio = usedRatio(summary);
  if (ratio === null || summary.monthly_limit_micros === null) return null;
  const state = budgetState(summary);
  return <div className="spend-meter">
    <div
      className={"spend-meter-track " + state.tone}
      role="meter"
      aria-valuemin={0}
      aria-valuemax={summary.monthly_limit_micros}
      aria-valuenow={Math.min(summary.consumed_micros, summary.monthly_limit_micros)}
      aria-valuetext={`${formatUnits(summary.consumed_micros)} of ${formatUnits(summary.monthly_limit_micros)} units used`}
    >
      <span className="spend-meter-fill" style={{ width: (ratio * 100).toFixed(1) + "%" }}/>
    </div>
    <p className="notice">{(ratio * 100).toFixed(1)}% of the monthly limit used this period.</p>
  </div>;
}

function Errors({ query }: { query: { budget_error?: string } }) {
  const messages: Record<string, string> = {
    invalid: "Enter an amount between 0 and 1,000,000,000 units with at most six decimals.",
    forbidden: "Only workspace owners and admins can change the budget.",
    failed: "The budget could not be saved. Check your current access and try again.",
  };
  const message = query.budget_error ? messages[query.budget_error] ?? messages.failed : null;
  return message ? <p role="alert">{message}</p> : null;
}

export default async function Spend({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string; category?: string; saved?: string; budget_error?: string }>;
}) {
  const { id } = await params;
  const query = await searchParams;
  const session = await currentSession();
  if (!session) redirect("/login");

  const category = query.category ? spendCategorySchema.safeParse(query.category) : null;
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !z.uuid().safeParse(query.cursor).success)
    || (category !== null && !category.success)) {
    return <section className="workspace-content"><h1>Invalid spend link</h1>
      <p role="alert">The requested identifier, filter or page cursor is invalid.</p>
      <a href="/workspaces">Back to workspaces</a></section>;
  }

  const root = `/workspaces/${id}/spend`;
  const filter = category?.success ? `&category=${category.data}` : "";
  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    const summary = await api(session, `/api/v1/workspaces/${id}/spend`, spendSummarySchema);
    const records = await api(
      session,
      `/api/v1/workspaces/${id}/spend/records?limit=25${filter}${query.cursor ? "&cursor=" + query.cursor : ""}`,
      spendRecordPageSchema,
    );
    const state = budgetState(summary);
    const calls = summary.categories.reduce((total, row) => total + row.call_count, 0);
    const canManage = workspace.role !== "member";

    return <section className="workspace-content evaluation-content">
      <a href={`/workspaces/${id}`}>← {workspace.name}</a>
      <p className="eyebrow">COST / CURRENT PERIOD</p><h1>Spend and budget</h1>
      <p className="intro">
        Model spend recorded by the worker for {spendPeriod(summary)}.
      </p>
      <p className="notice">
        Amounts are accounting units of your operator&apos;s currency, stored exactly as
        millionths (micros) and priced when each call was made. Model generation, quality
        judging and retrieval or ingestion embeddings are all metered here.
      </p>

      {query.saved === "budget" && <p role="status">The budget was saved.</p>}
      <Errors query={query}/>

      <div className="card spend-panel">
        <p className={"state " + state.tone}>{state.label}</p>
        <dl className="eval-counts spend-counts">
          <div><dt>Consumed</dt><dd>{formatUnits(summary.consumed_micros)}</dd></div>
          <div><dt>Monthly limit</dt>
            <dd>{summary.monthly_limit_micros === null ? "—" : formatUnits(summary.monthly_limit_micros)}</dd></div>
          <div><dt>Remaining</dt>
            <dd>{summary.remaining_micros === null ? "—" : formatUnits(summary.remaining_micros)}</dd></div>
          <div><dt>Priced calls</dt><dd>{calls}</dd></div>
        </dl>
        <SpendMeter summary={summary}/>
        <p className="notice">Exact recorded total: {formatMicros(summary.consumed_micros)}.</p>
        {summary.exhausted && <p role="alert">
          New model calls in this workspace are refused until the next period or a higher limit.
          Agent runs fail with <code>workspace_budget_exhausted</code> before reaching a provider.
        </p>}
        {summary.enforcement === "monitor" && <p className="notice">
          Enforcement is set to monitor: spend is recorded and reported, but no call is blocked.
        </p>}
        {summary.monthly_limit_micros === null && <p className="notice">
          No budget is set. Spend is recorded but nothing stops this workspace from spending more.
        </p>}
      </div>

      <h2 className="eval-section-title">Where it went</h2>
      {summary.categories.length === 0
        ? <div className="empty"><h3>No priced model calls this period</h3>
          <p>Costs appear here once the worker records a priced provider call for this workspace.</p></div>
        : <table className="spend-table spend-categories">
          <caption>Recorded spend by category for the current period.</caption>
          <thead><tr>
            <th scope="col">Category</th><th scope="col">Calls</th>
            <th scope="col">Input tokens</th><th scope="col">Output tokens</th><th scope="col">Cost</th>
          </tr></thead>
          <tbody>{summary.categories.map(row => <tr key={row.category}>
            <th scope="row">{CATEGORY_LABELS[row.category]}</th>
            <td data-label="Calls">{row.call_count}</td>
            <td data-label="Input tokens">{row.input_tokens.toLocaleString("en-GB")}</td>
            <td data-label="Output tokens">{row.output_tokens.toLocaleString("en-GB")}</td>
            <td data-label="Cost">{formatUnits(row.cost_micros)}</td>
          </tr>)}</tbody>
        </table>}

      <h2 className="eval-section-title">Monthly budget</h2>
      {canManage
        ? <form className="workspace-form spend-form" action="/workspaces/spend/budget" method="post">
          <p>
            The worker checks the remaining budget before each provider call. The call that
            crosses the limit is the last one allowed, so concurrent runs can overshoot slightly.
          </p>
          <input type="hidden" name="csrf" value={session.csrf}/>
          <input type="hidden" name="workspace" value={id}/>
          <label htmlFor="limit">Monthly limit (accounting units)</label>
          <input
            id="limit" name="limit" required inputMode="decimal" autoComplete="off"
            pattern="\d{1,10}([.,]\d{1,6})?"
            defaultValue={microsToUnits(summary.monthly_limit_micros ?? 0)}
            aria-describedby="limit-help"
          />
          <p id="limit-help" className="notice">
            Up to six decimals. A limit of 0 with enforcement stops every model call in this
            workspace, including quality judges.
          </p>
          <label htmlFor="enforcement">Enforcement</label>
          <select id="enforcement" name="enforcement" defaultValue={summary.enforcement ?? "enforce"}>
            <option value="enforce">Enforce · refuse calls once the limit is reached</option>
            <option value="monitor">Monitor · record spend without blocking</option>
          </select>
          <button type="submit">Save budget</button>
        </form>
        : <p className="notice">
          You have read-only access to this workspace. Only owners and admins can change the budget.
        </p>}

      <h2 className="eval-section-title">Priced calls</h2>
      <nav className="spend-filters" aria-label="Filter priced calls">
        <a href={root} aria-current={category === null ? "page" : undefined}>All</a>
        {spendCategorySchema.options.map(option => <a
          key={option}
          href={`${root}?category=${option}`}
          aria-current={category?.success && category.data === option ? "page" : undefined}
        >{CATEGORY_LABELS[option]}</a>)}
      </nav>
      {records.items.length === 0
        ? <div className="empty"><h3>No priced calls on this page</h3>
          <p>Each recorded call appears here with the work that produced it.</p></div>
        : <table className="spend-table spend-ledger">
          <caption>Every priced call, newest first. Each row is written once and never changed.</caption>
          <thead><tr>
            <th scope="col">Recorded</th><th scope="col">Category</th><th scope="col">Model</th>
            <th scope="col">Tokens</th><th scope="col">Cost</th><th scope="col">Source</th>
          </tr></thead>
          <tbody>{records.items.map(record => <tr key={record.id}>
            <td data-label="Recorded">
              <time dateTime={record.occurred_at}>{spendTimestamp(record.occurred_at)}</time></td>
            <td data-label="Category">{CATEGORY_LABELS[record.category]}</td>
            <td data-label="Model">{record.provider}/{record.model}</td>
            <td data-label="Tokens">{(record.input_tokens + record.output_tokens).toLocaleString("en-GB")}</td>
            <td data-label="Cost">{formatUnits(record.cost_micros)}</td>
            <td data-label="Source"><code>{record.source_key}</code></td>
          </tr>)}</tbody>
        </table>}
      <nav className="pagination" aria-label="Priced call pages">
        {query.cursor && <a href={root + (filter ? "?" + filter.slice(1) : "")}>Newest calls</a>}
        {records.next_cursor && <a
          href={`${root}?cursor=${records.next_cursor}${filter}`}
        >Older calls →</a>}
      </nav>
    </section>;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) redirect("/login?error=session_expired");
    const denied = error instanceof ApiError && [403, 404].includes(error.status);
    return <section className="workspace-content evaluation-content">
      <h1>{denied ? "Spend not found or access denied" : "Spend unavailable"}</h1>
      <p role="alert">{denied
        ? "Check your workspace access and try again."
        : "The spend service could not be reached. Try again shortly."}</p>
      <a href={`/workspaces/${id}`}>Back to workspace</a>
    </section>;
  }
}
