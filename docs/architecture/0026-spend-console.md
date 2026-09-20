# ADR 0026: Spend and budget console

Status: implemented.

## Context

ADR 0025 meters provider spend and enforces per-workspace budgets, but the only way to read a
period total or change a cap is the API. A budget that is invisible until a run fails with
`workspace_budget_exhausted` is an operational trap: the people who pay for the workspace cannot
see what it costs, and the people who can raise the cap need a browser to do it.

Money numbers also fail differently from other UI data. A total that disagrees with its own
breakdown, a limit rendered with floating-point drift, or an amount rounded on the way to the API
would all be wrong in a way the reader cannot detect.

## Decision

Add a server-rendered `/workspaces/{id}/spend` page and a CSRF-protected mutation route for the
budget, both built on the endpoints ADR 0025 already exposes. No new API surface is required.

The page shows the current period window, consumed and remaining amounts, the budget state, the
per-category breakdown and the priced-call ledger with cursor pagination and a category filter.
Owners and admins additionally get the budget form; members see the same numbers and an explicit
read-only notice.

## Exact amounts in the browser

The ledger stores micros — millionths of one unit of the operator accounting currency. The console
never introduces a currency symbol, because the API does not state one.

Both conversions are integer arithmetic over `BigInt`, not float division:

- micros render as units for reading, alongside the exact micro total for auditing;
- the budget form accepts units with at most six decimals and converts them back to exact micros.

An amount that cannot be represented exactly is refused twice: by the input pattern in the browser
and again in the mutation route, which never rounds a submitted value to make it fit.

## Contract invariants

The typed contract rejects a summary that cannot be true, rather than rendering it:

- the period total must equal the sum of its own category rows;
- a limit and an enforcement mode are present together or not at all;
- remaining must equal the limit minus consumed, floored at zero;
- `exhausted` is reachable only under `enforce`, never in monitor mode.

A response failing any of these is an error state, not a dashboard.

## States shown explicitly

Unmetered (no budget), within budget, approaching the limit, exhausted under enforcement, and over
the limit under monitoring are distinct, each written in words next to its colour so the meter is
never the only carrier of the message. The exhausted state names the failure code operators will
see on runs. The budget form warns that a limit of zero with enforcement stops every model call in
the workspace, including quality judges.

Empty ledgers, an invalid cursor or category, a denied workspace and an unreachable spend service
each render their own state instead of an empty table.

## Browser boundary

The browser never receives the API access token. The budget form posts the workspace ID, the
amount, the enforcement mode and the session CSRF token to a Next.js route, which validates origin,
referer and CSRF before calling the API with the server-held token. Permission is still enforced by
the API: a member who forges a request gets 403 from the platform, not from the hidden form.

The page holds no client state and performs no polling; the URL carries the cursor and filter.

## Limits

The console reports the current UTC month only. Historical periods, export and spend alerts are not
part of this increment, and embedding calls remain unmetered upstream, which the page states rather
than implying a complete bill.

## Verification

- unit coverage for the summary invariants, record-page bounds, and exact unit/micro conversion in
  both directions including round trips at the maximum limit;
- browser coverage for the empty console, populated totals and breakdown, ledger pagination and
  category filtering, saving a budget, the exhausted and monitor states, an invalid amount refused
  by both the input pattern and the server route, invalid cursor and category links, an unavailable
  spend service, member read-only access and a forged budget post rejected with 403;
- responsive checks at 1440px and 390px with no horizontal page scroll.

## Skills applied

nextjs-frontend, cost-performance, auth-rbac-multitenancy, api-openapi, security-threat-modeling,
testing-quality.
