---
name: observability-otel
description: Use when adding OpenTelemetry traces, metrics, logs, Prometheus metrics, dashboards, SLOs, run tracing, or production diagnostics.
---

# Observability

A production agent platform must explain what happened.

## Trace
Propagate trace_id across HTTP, workers, model calls, retrieval and tools.
Create spans for model, retrieval, policy, approval and tool execution.

## Metrics
Track request/run latency, errors, token usage, cost, queue depth, tool failures, provider failures and approval wait time.

## Logging
Use structured logs with correlation IDs. Never log secrets or raw sensitive prompts by default.

## SLOs
Define user-facing SLOs for API availability and agent-run reliability before creating dashboards.
