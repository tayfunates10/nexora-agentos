# ADR 0004: Agent definitions, durable runs and transactional outbox

Status: implemented for persistence and dispatch; model execution and MCP tools remain disabled.

## Scope

This milestone adds workspace-scoped agent definitions and durable agent-run records without
introducing an unconstrained model loop. A run is a persisted state machine aggregate. Creating
a run records its initial `queued` event and an outbox job in the same PostgreSQL transaction.
No API request invokes a model provider or privileged tool directly.

Owners and admins may create agent definitions. Owners, admins and members may start runs and
read agents/runs in workspaces where they currently hold membership. Every request revalidates
membership in PostgreSQL; a token claim never grants a workspace role. Cross-workspace agent IDs
are rejected without revealing whether the foreign agent exists.

## Durable run contract

Each run has stable `workspace_id`, `agent_id`, requester identity, `run_id` and `trace_id`.
The database constrains the planned lifecycle states:

`queued -> running -> waiting_for_approval -> queued -> succeeded|failed|cancelled`

The worker transition rules are deliberately not implemented in this milestone. Future worker
code must reject illegal transitions and terminal-run execution rather than relying on callers.
Run events are append-only and ordered by `event_no`; database triggers reject updates, deletes,
and truncation.

The initial run request is stored server-side for future workers. It is not included in the
outbox payload, logs, or the API run response. The outbox carries only identifiers required to
load authorized durable state.

## Idempotency and outbox delivery

`POST /api/v1/workspaces/{workspace_id}/runs` requires `Idempotency-Key`. The key is unique per
workspace and requesting identity. Replaying the same key and normalized request returns the
existing run; reusing the key for different content returns HTTP 409.

Run, initial event, security audit, and `agent.run.queued.v1` outbox job commit atomically.
`OutboxPublisher` claims pending jobs with `FOR UPDATE SKIP LOCKED` and publishes them to the
Redis queue `nexora:jobs:agent-runs`. Delivery is **at least once**: a process crash after Redis
accepts a job but before PostgreSQL records `published_at` can cause a duplicate. Every job has
a stable `job_id`; the future worker must persist deduplication before side effects.

Publishing retries use bounded exponential backoff and move a job to dead-letter state after ten
failed attempts. Stored failure diagnostics contain only the exception class, not connection
strings or credentials.

## Trust boundaries and remaining work

The API authenticates the user and authorizes workspace membership. PostgreSQL is the durable
source of truth. Redis is only the delivery queue and may be replayed from the outbox.

The next milestone implements the worker state machine, legal transition checks, cancellation,
recovery, and idempotent job consumption. MCP tools, policy evaluation, durable human approvals,
model/provider adapters and model execution remain disabled until their dedicated milestones.

## Skills applied

agent-architecture, fastapi-backend, postgres-data-modeling, redis-jobs,
event-driven-workflows, auth-rbac-multitenancy, api-openapi, testing-quality.
