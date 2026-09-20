# ADR 0025: Workspace spend accounting and budget enforcement

Status: implemented.

## Context

Nexora records provider token usage on runs, run events, evaluation judge scores and bounded
Prometheus counters, but nothing converts that usage into money or stops a workspace from spending
without limit. ADR 0007 left provider spend accounting as follow-up work and ADR 0024 stated that
monetary conversion remains operator pricing data. The cost-performance skill requires cost tracked
per run, agent and workspace; the model-routing skill requires per-workspace budgets to be enforced.

Token counters alone cannot answer "what did this workspace cost this month", because prices change
independently of history and Prometheus deliberately carries no tenant labels.

## Decision

Nexora adds a durable, append-only spend ledger plus per-workspace monthly budgets.

- Operators declare per-model prices in the worker runtime configuration. A run, an agent, a tenant
  and a model response cannot influence a price.
- A priced provider call is converted to cost once, at the moment its result is committed, and
  stored in `workspace_spend_records`. A later price change never rewrites recorded history.
- Owners and admins set a monthly limit and an enforcement mode per workspace through the API.
- The worker checks the remaining budget before provider egress. In `enforce` mode an exhausted
  budget stops the call; in `monitor` mode the overage is reported but execution continues.
- Members can read their workspace's period usage and the priced ledger; only `spend:manage`
  holders can change a cap.

## Units and pricing

Cost is an integer number of micros: one micro is a millionth of one unit of the operator
accounting currency. Prices are declared per million tokens, separately for input and output, and
cost is computed with integer arithmetic that rounds a partial price unit up, so accounting never
silently under-reports. Floating point is not used anywhere in this path.

A deployment prices every configured model candidate or none of them. Partial pricing would meter
some provider egress and silently exempt the rest, so the worker refuses to start with it. Without
prices, accounting and budget enforcement stay off rather than recording a fabricated zero cost; a
model that is routed but not priced fails the run closed with `model_price_not_configured` instead
of executing unmetered.

A deployment uses a single accounting currency. Multi-currency ledgers and currency conversion are
deliberately out of scope: a stored number whose currency can change is not evidence.

## Exactly-once accounting

Every ledger row carries a deterministic `source_key` — `agent-run:{run_id}:step:{n}` for a model
step and `eval-judge:{judge_run_id}:case:{case_id}` for a judge case — unique per workspace. The
row is written in the same transaction that commits the work it prices: the agent model step, or
the judge case score. A replayed step, a resumed run, a requeued judge job and crash recovery
therefore cannot charge the same unit of work twice.

The inverse boundary is the one ADR 0022 already documents: a process that dies after provider
success but before its transaction commits loses both the durable result and its ledger row. That
call is undercounted rather than double-counted, which is the safer direction for a record that
authorizes nothing.

## Enforcement boundary

The accounting period is the UTC calendar month, derived from the database clock so a worker with a
skewed process clock cannot shift a tenant's window.

The budget check runs before provider egress, not after billing data arrives, and it is a gate
rather than a hard cap:

- the call that crosses the limit is the last one allowed, and its cost is bounded by the execution
  profile's `max_output_tokens` and `max_total_tokens`;
- concurrent runs in one workspace each read committed spend, so parallel execution can exceed a
  limit by the cost of the calls already in flight. Serializing every run behind one budget row
  would trade a bounded overshoot for a tenant-wide execution bottleneck.

A denied agent run fails terminally with `workspace_budget_exhausted` and appends a `spend.denied`
run event carrying the limit and the consumed amount, so the tenant sees why the run stopped. A
denied judge job fails with `judge_budget_exhausted` without provider egress. Neither path invents
token usage or cost for a call that never happened.

Budgets do not grant permission. Execution still requires workspace membership, an authorized model
profile and, for tools, policy and approval. A budget can only subtract.

## Data model

- `workspace_spend_records`: append-only ledger keyed by `(workspace_id, source_key)`, indexed for
  newest-first keyset pagination and period aggregation.
- `workspace_spend_budgets`: current limit and enforcement mode per workspace.
- `workspace_spend_budget_events`: append-only history of every budget decision with actor and
  request identity, because raising a cap authorizes future spending.

Update, delete and truncate are rejected by triggers on both append-only tables. Budget changes are
additionally recorded in `security_events` through the existing workspace audit path.

## API contract

- `GET /api/v1/workspaces/{workspace_id}/spend` — period window, consumed micros, limit,
  enforcement, remaining micros, an exhausted flag and per-category totals. Requires
  `workspace:read`.
- `PUT /api/v1/workspaces/{workspace_id}/spend/budget` — idempotent limit and enforcement update.
  Requires `spend:manage`, which owners and admins hold.
- `GET /api/v1/workspaces/{workspace_id}/spend/records` — cursor-paginated priced calls, optionally
  filtered by category. Requires `workspace:read`. A cursor belonging to another workspace is a 404,
  not a cross-tenant read.

The API never calls a provider and never holds pricing data; it reports what the worker recorded.

## Observability

Two bounded series are added: `nexora_model_cost_micros_total{provider,category}` and
`nexora_spend_denials_total{category}`. Category is restricted to `agent_run` and
`evaluation_judge`; unknown values collapse to `other`. Workspace, run, user and case identifiers
stay out of Prometheus, exactly as in ADR 0010 and ADR 0024. Per-tenant cost is answered from the
durable ledger, which is permission-scoped, rather than from metrics, which are not.

## Limits

Embedding calls made for retrieval and knowledge ingestion are not priced in this increment, so the
ledger covers model generation only and a budget bounds generation spend only. Making ingestion and
retrieval embeddings billable units is the next step; until then the API and this document state
which categories are metered instead of implying a complete bill.

There is no deployment-wide default cap: a workspace without a budget row is metered but unlimited.
A second source of truth in worker configuration would report a limit the API could not see.

Budget exhaustion is terminal for the affected run. Automatic resumption when the next period
starts, spend forecasting and alert thresholds are not part of this increment.

## Verification

- unit coverage for integer cost rounding, invalid prices, unpriced models, the full budget decision
  matrix, bounded source keys, all-or-none pricing configuration and bounded metric labels;
- PostgreSQL/API integration coverage for RBAC on budget changes, member-readable summaries,
  cross-tenant record and cursor isolation, budget history, audit records and append-only rejection;
- worker integration coverage proving a real run writes exactly one priced ledger row per model
  step, that a replayed step is not charged twice, that an exhausted budget stops the run before any
  provider request, and that an unpriced model fails closed;
- judge integration coverage proving candidate and baseline scoring are priced into the ledger and
  that an exhausted budget denies scoring before egress.

## Skills applied

cost-performance, model-routing, postgres-data-modeling, auth-rbac-multitenancy, api-openapi,
fastapi-backend, observability-otel, security-threat-modeling, testing-quality,
code-review-debugging.
