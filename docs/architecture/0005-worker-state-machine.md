# ADR 0005: Leased worker state machine and Redis Streams

Status: implemented for orchestration; model/provider execution remains intentionally unconfigured.

## Decision

Agent jobs use Redis Streams with a consumer group instead of destructive list pops. PostgreSQL
remains the durable source of truth. Redis delivery is at least once and every message is
revalidated against its PostgreSQL outbox row before a worker changes run state.

A worker claims a queued run by atomically moving it to running, incrementing attempt_count,
persisting a worker receipt and assigning a bounded lease. The executor runs outside the
database transaction. A heartbeat renews both run and receipt leases. Completion is fenced by
worker identity: a worker that lost its lease cannot later overwrite the state chosen by a
replacement worker.

## State transitions

The database trigger permits only queued -> running/cancelled, running ->
queued/waiting_for_approval/succeeded/failed/cancelled, and waiting_for_approval ->
queued/cancelled. Terminal states remain terminal. Same-state updates are allowed for metadata
such as cancellation requests and lease renewal.

Run events remain append-only. Worker events include run.started, run.retry_scheduled,
run.recovered, run.redis_requeued, run.succeeded, run.failed and run.cancelled.

## Retry and recovery

Retryable executor errors use a maximum of three run attempts with deterministic exponential
backoff plus jitter. A retry creates a fresh transactional outbox row; the consumed job receipt
becomes terminal. Unknown executor exceptions are normalized to executor_error and follow the
same bounded retry path so stack traces and secrets are not persisted.

Redis pending messages are reclaimable after the worker lease expires. Workers also reconcile
PostgreSQL before consuming: expired running leases are returned to queued and receive a fresh
outbox job. Old processing receipts are marked superseded. Queued runs whose published delivery
may have disappeared from ephemeral Redis are conservatively re-enqueued after a safety window.
Duplicate deliveries are harmless because receipts and run locks are checked before execution.

## Cancellation

The requester may cancel their own run. Owners and admins may cancel any run in their workspace.
Queued or waiting runs become cancelled immediately. Running runs receive cancel_requested_at;
the executor can poll the provided cancellation callback, and finalization converts a requested
run to cancelled instead of succeeded. Cancellation is idempotent and authorization uses current
database membership, not token role claims.

## Executor boundary

AgentWorker accepts a RunExecutor protocol. This milestone deliberately does not register a
production model executor and does not add a worker service to Compose. Starting a fake model
worker would violate the provider boundary. A later provider-adapter milestone can supply the
executor without changing queue, lease, retry, cancellation or recovery semantics.

No tool execution or external side effect is enabled. Future tool/MCP operations must add their
own durable idempotency keys and approval/policy gates because worker-level at-least-once
execution cannot make arbitrary external side effects idempotent.

## Skills applied

agent-architecture, event-driven-workflows, redis-jobs, postgres-data-modeling,
security-threat-modeling, testing-quality, observability-otel.
