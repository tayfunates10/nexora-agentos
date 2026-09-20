# ADR 0010: Durable run traces, bounded metrics and structured logs

Status: accepted for the observability milestone.

## Trace identity
A run's `agent_runs.trace_id` UUID is used directly as the OpenTelemetry trace id, and
the parent span id is derived deterministically from `run_id`. API, worker, retrieval,
model and tool spans therefore belong to one trace without carrying trace context
through the job queue, so the durable job payload contract stays byte-for-byte
comparable. Redelivery, retry and approval resume reuse the same trace; `nexora.attempt`
separates the attempts.

Head sampling hashes the trace id instead of masking its low bits, because UUID version
and variant bits are fixed and would otherwise make a ratio below 1.0 select nothing.
Every process derives the same decision for the same run, so traces are never partial.

Inbound `traceparent` headers are accepted for correlation only. Trace context is never
an identity, a tenant claim or an authorization input. Outbound provider requests do not
carry trace headers; correlation stays inside the platform.

## What telemetry may contain
Span attributes are allowlisted and bounded, and log context fields are allowlisted the
same way; unknown keys are dropped rather than emitted. Prompts, tool arguments, tool
results, retrieved context, tokens and credentials are never recorded. Telemetry carries
identifiers, counts, policy actions and error codes, which is what an operator needs to
explain a run without reading tenant content.

## Metric cardinality
Metrics use a dedicated registry. Workspace, user, run, approval and tool names are
high-cardinality tenant data and stay on spans; metric labels are limited to HTTP method,
matched route template, status, run outcome, provider, MCP server key, token kind,
approval decision and fixed evaluation-judge target/outcome values. Unmatched paths collapse to `unmatched`, operator label values that do
not match the bounded pattern collapse to `other`, and unknown outcomes collapse to
`other`. Approval latency is observed exactly once, where the pending approval actually
transitions, so retried or repeated decision requests cannot inflate the SLO signal. That
observation happens inside the transaction, so a rollback after the transition can leave
one extra count; telemetry never blocks or alters the decision itself.

## Exposure and egress
`/metrics` is disabled unless an operator sets a scrape token of at least 32 characters:
unset returns 404, a wrong token returns 401, and scrape traffic is excluded from the
HTTP metrics it serves. Without a configured collector endpoint no span leaves the
process, and the endpoint must be an operator-supplied http(s) URL. Exporter, sampling,
scrape token and log level are process configuration, never workspace or model input.

## Starting SLOs
Two user-facing targets are declared in code before any dashboard exists, over a rolling
30-day window: API availability at 99.9% of eligible requests served without a platform
5xx, and agent run reliability at 99% of eligible runs reaching a successful terminal
state without a platform failure. They are exported as `nexora_slo_objective_ratio` and
`nexora_slo_window_days`, so alert rules read the stated goal instead of hardcoding a
number, and the error budget follows from the objective rather than a second constant.

These are initial engineering objectives; production traffic and an error-budget policy
must precede any contractual guarantee. The supporting signals are latency of `POST /runs`
and the read endpoints, run outcome, governed tool failure rate by server key, and human
approval wait time. Dashboards and alert tuning are deployment work and are not part of
this increment. Worker-side evaluation judge calls are additionally covered by the bounded
series defined in ADR 0024; they reuse the same provider label policy and never add tenant IDs.

## Boundaries
Metrics are per-process, so each API or worker replica is scraped separately; the worker
is still a library and exposes its instruments through the process that embeds it until a
worker service exists. Traces are exported over OTLP/HTTP; metrics are Prometheus-scraped
rather than pushed. The Next.js tier is not instrumented yet, so a browser request is
correlated only from the API boundary inward. Collector deployment, dashboards, alert
rules and log shipping remain deployment concerns.

## Skills applied
observability-otel, security-threat-modeling, cost-performance, fastapi-backend,
testing-quality.

## References
- https://www.w3.org/TR/trace-context/
- https://opentelemetry.io/docs/specs/otel/trace/sdk/#sampling
- https://prometheus.io/docs/practices/naming/
