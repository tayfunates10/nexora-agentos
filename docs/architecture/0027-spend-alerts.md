# ADR 0027: Budget thresholds and spend alerts

Status: implemented.

## Context

ADR 0025 meters every provider call and stops a workspace at its monthly limit. That is a
backstop, not a warning: the first signal a tenant gets is a run failing with
`workspace_budget_exhausted`, at the moment there is nothing left to decide. The ledger already
holds what would be needed to warn earlier, but nothing watches it, and a naive watcher would
repeat the same warning on every call for the rest of the month.

## Decision

Owners and admins declare alert thresholds as part of the budget: up to five whole percentages of
the monthly limit. When a recorded call takes the period's spend to a threshold, that crossing is
written once to an append-only `workspace_spend_alerts` row and exposed through the spend API,
the console and a bounded Prometheus counter.

Alerts are advisory. They never block a call, never change enforcement and never alter a budget;
enforcement alone decides whether work proceeds.

## Once per period, decided by the database

A threshold alert carries `(workspace_id, period_start, threshold_percent)` as a unique key. The
evaluation statement inserts every reached threshold and lets the conflict clause discard what
already exists, so a workspace that makes ten thousand calls after crossing 80% stores one 80% row.
No worker state, cache or scheduler is involved in that guarantee.

Uniqueness does not prevent a missed crossing: concurrent transactions could each see only their
own sub-threshold spend. Before evaluating, each transaction locks its budget row with
`FOR NO KEY UPDATE` until commit or rollback. The aggregate runs in a separate statement under
READ COMMITTED, so a waiting evaluator sees the preceding committed spend. The lock is scoped
to one workspace budget and remains compatible with foreign-key key-share locks. Rolled-back
spend never contributes to the crossing.

The evaluation runs inside the same transaction as the ledger row that caused it, so an alert
cannot exist for spend that was rolled back, and a spend row cannot commit without its crossing
being considered. It also runs when a budget is written, because lowering a limit can put a period
past a threshold without spending another micro.

Crossing is integer arithmetic — `consumed * 100 >= threshold * limit` — never a division. At the
top of the allowed range a float comparison reports a crossing one micro early; the regression test
pins the exact case.

## What an alert records

An alert stores the limit, the consumed amount and the enforcement mode as they stood at the
crossing. A later budget change does not rewrite it: the row answers "what was true when this fired",
which is the question an operator reviewing a month actually has. Rows reject update, delete and
truncate at the database, like the ledger itself.

Thresholds are normalized to a sorted, unique set before storage, so one crossing can never produce
two alerts, and the stored array is bounded to five entries between 1 and 100 by a check constraint.

## Surfaces

- `GET /spend` returns the configured thresholds and the alerts raised in the current period, both
  readable by any workspace member.
- `PUT /spend/budget` accepts `alert_thresholds`; an omitted list defaults to 80% and 100%, and an
  explicit empty list turns alerting off. Changing it requires `spend:manage`, and the change is
  recorded in the append-only budget history alongside the limit it belongs to.
- The console shows reached thresholds with the amounts they fired at, offers the standard
  percentages as checkboxes plus any value an operator set through the API, and says plainly when a
  budget has no thresholds at all.
- `nexora_spend_alerts_total{threshold}` counts first crossings. The label is a whole percent, so
  the series count is bounded by definition, and no tenant identifier enters Prometheus.

## Limits

This increment records and surfaces alerts; it does not deliver them. There is no email, webhook or
chat notification, because an outbound channel needs its own egress, secret and replay boundary —
the transactional outbox is the natural carrier when that increment comes, and an alert row is
already the durable trigger it would read.

An alert is not re-armed within a period: crossing 80%, dropping below it through a raised limit and
crossing it again records one alert. A new accounting month starts with none. Thresholds apply to
the workspace total, not per category, and there is no per-agent or per-user threshold.

## Verification

- unit coverage for threshold normalization, rejected values, the default set, and the crossing
  comparison at the boundary, at a zero limit and at the exact amount where a float comparison
  would alert one micro early;
- integration coverage proving a threshold fires at the crossing and not one micro before, that
  further spending in the same band does not repeat it, that a tighter budget raises the next
  threshold without new spend, that an alert keeps the amounts it fired at, that append-only
  mutation is refused, and that a workspace without thresholds never alerts;
- concurrent PostgreSQL transactions proving two sub-threshold writes raise one alert after
  commit, a rolled-back write does not contribute, and replay does not charge or alert again;
- browser coverage for selecting thresholds, reading raised alerts with their amounts, the
  no-thresholds notice, clearing and re-adding thresholds, and alerts not repeating on a second
  budget save.

## Skills applied

cost-performance, postgres-data-modeling, auth-rbac-multitenancy, api-openapi, nextjs-frontend,
observability-otel, testing-quality, code-review-debugging.
