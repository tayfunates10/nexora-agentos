# Observability foundation

Nexora's observability boundary follows `skills/observability-otel/SKILL.md`.

## SLOs

Before dashboards or alert tuning, the platform defines these initial user-facing 30-day targets:

- API availability: 99.9% of eligible API requests are served without platform 5xx failures.
- Agent-run reliability: 99% of eligible runs reach a successful terminal state without a platform failure.

These targets are initial engineering objectives and require production traffic/error-budget policy
before they become contractual guarantees.

## Correlation and privacy

`request_id` remains the HTTP correlation identifier. Runtime instrumentation may additionally
propagate `trace_id`, `workspace_id`, `run_id`, and `agent_id`. Structured telemetry is
allowlist-based. Raw prompts, model responses, authorization headers, credentials, tool arguments,
retrieved document contents, and approval payloads must not be logged or attached to spans by
default.

## Planned spans and metrics

Trace boundaries: HTTP, worker run, provider/model call, retrieval, policy evaluation, approval wait,
and tool execution. Metrics will cover latency/errors, run reliability, queue depth, provider/tool
failures, token usage/cost when supplied by a provider, and approval wait duration.

Exporter wiring is deliberately separate from these primitives so tests and local development do not
require a telemetry backend. Production exporters must use fixed operator configuration rather than
tenant/model-controlled endpoints.
