# ADR 0032: Spend alert delivery through a transactional outbox

Status: implemented.

## Context

ADR 0027 records a budget threshold crossing once per period and shows it in the console. That is
enough for someone already looking at the page, which is not the person a budget warning is for.
Delivery was deliberately left out of that increment because an outbound request is a different
kind of capability from a database row: it leaves the trust boundary, it can be aimed, it can be
forged, and it can be retried into a storm.

## Decision

Add a transactional outbox for alert notifications and a worker-side deliverer for it.

The crossing and the intent to notify are written in the same statement: the insert that records an
alert also inserts its outbox row. A notification can never be owed for a crossing that was rolled
back, and a crossing can never commit without its notification being queued. The outbox row's
primary key is the alert id, so one crossing can only ever produce one notification.

Delivery itself is a separate, opt-in worker. Without an operator endpoint the rows simply stay
queued: nothing is dropped because delivery is not configured yet.

## The endpoint is operator configuration, never tenant input

A workspace cannot name a URL. The endpoint is declared in the worker runtime configuration, like
MCP servers, and validated with the same rules: HTTPS only, port 443, no credentials, query or
fragment in the URL, no `localhost`, and no literal IP outside the globally routable range. The
worker refuses to start on anything else.

This is the control that matters most here. A tenant-supplied webhook URL would turn a budget
alert into a request the platform makes on a tenant's behalf to an address of their choosing —
the classic pivot into internal networks and metadata services. There is no such path.

Redirects are never followed: a 3xx response abandons the notification rather than moving a signed
tenant event to an unvetted host. The response body is drained under a 16 KiB bound and never
parsed, so a hostile or broken receiver cannot feed the worker anything or hold it open.

## Signing and replay

Every request carries `nexora-timestamp` and `nexora-signature: v1=<hmac>`, an HMAC-SHA256 over
`timestamp + "." + body` using a secret that lives in the worker environment and only appears in
configuration by variable name. A receiver verifies the exact bytes it was sent before parsing
them, and rejects a stale timestamp; without that, anyone who learns the URL can forge a budget
alert. A secret shorter than 32 characters stops the worker from starting.

`nexora-delivery-id` carries the alert id so a receiver can deduplicate: delivery is at-least-once
by design, because a crash between a successful POST and its commit retries the request.

## What is delivered

The event is `spend.threshold.reached.v1` and carries identifiers and amounts only: alert id,
workspace id, period start, threshold, limit, consumed micros and enforcement mode. No workspace
name, no user identity, no prompt, model, tool or retrieval data crosses the boundary — the same
rule the metrics layer follows, applied to a channel that leaves the platform entirely.

## Failure handling

Each attempt takes a lease, so two workers cannot deliver the same notification concurrently. A
retryable failure — timeout, transport error, 408, 425, 429 or 5xx — backs off with deterministic
jitter. Anything else, including a redirect or a 4xx rejection, is abandoned immediately: retrying
a request the receiver refused only repeats it.

After five attempts a notification is dead-lettered with its last error rather than retried
forever, and the outcome is counted in `nexora_spend_alert_deliveries_total{outcome}` and logged
once. Delivered and abandoned are both terminal at the database: a trigger refuses to re-deliver or
re-abandon a row, so a lost lease cannot resend a notification a receiver already accepted.

## Tenant boundary

Delivery state is operator infrastructure and stays out of the tenant API. A workspace member sees
the durable alert in the console; whether the operator's webhook accepted it is not their concern
and would leak operator detail into a tenant surface.

## Database least privilege

Migration 014 is part of the production PostgreSQL role policy. The API role may INSERT an outbox row
when a budget edit crosses a threshold. The worker role may INSERT notifications produced by metered
provider work and UPDATE leased delivery state. Both inherit the common SELECT grant; neither owns
the table or receives DELETE, TRUNCATE, REFERENCES or TRIGGER privileges. The migration identity
retains schema ownership and deployment fails closed if this table is missing from the reviewed
runtime privilege inventory.

## Limits

One endpoint per deployment, not per workspace: per-tenant routing needs a tenant-configurable
destination, which is exactly the capability this ADR refuses to add without a separate
threat model. Email and chat channels are not implemented; a receiver that fans out to them is the
intended integration point. Dead-lettered notifications are not replayed automatically — an
operator who fixes their endpoint re-arms them deliberately.

## Verification

- unit coverage for endpoint validation (plaintext, loopback, private, link-local, documentation
  and IPv6 loopback addresses, credentials, ports, query and fragment), weak signing secrets,
  unbounded timeouts, payload contents, signature binding to body, timestamp and secret, and
  bounded deterministic retry delays;
- integration coverage proving a crossing queues exactly one notification, that delivery sends one
  signed request whose signature a receiver can recompute from the exact bytes, that the row
  becomes terminal and the database refuses a second delivery, that a redirect or rejection is
  abandoned without retrying, that an unavailable receiver is retried under backoff and abandoned
  after the attempt limit, and that an unconfigured deployment leaves notifications queued rather
  than dropping them.

## Skills applied

event-driven-workflows, security-threat-modeling, cost-performance, postgres-data-modeling,
observability-otel, testing-quality, code-review-debugging, docker-kubernetes.
