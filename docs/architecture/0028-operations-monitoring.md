# ADR 0028: Tested operational dashboards and SLO alerts

Status: implemented for the opt-in local operations stack.

## Problem

API and worker metrics exist, including declared SLOs and spend counters, but the repository
has no collector configuration, dashboard or tested operational alert rules. A metrics endpoint
alone cannot tell an operator when the platform is failing.

## Decision

Add a separate Compose overlay with version-pinned, non-root Prometheus and Grafana. Persistent
volumes hold their data; configuration is read-only, privileges are dropped and host ports bind
to loopback. Environment-backed Compose secrets supply the scrape and Grafana credentials through
files. No secret value enters version control or the dashboard. Anonymous dashboard access and
public signup are disabled. Grafana telemetry/update checks are disabled.

Prometheus scrapes API and worker separately. Four recording rules compute API and terminal-run
error ratios over five minutes and one hour. Errors are divided by request-weighted counts across
replicas, with rate computed before sum to preserve counter reset handling. Missing error series
become zero only when a real denominator exists; idle services do not fabricate success.

Three alerts cover target scrape failure and fast error-budget burn for the two exported SLOs.
Burn thresholds derive from the exported objective and window, require both windows to exceed
the threshold, and persist for two minutes. Neither tenant identities nor unbounded paths enter
alert labels. A ten-panel Grafana dashboard shows the underlying operational evidence.

The Redis stream gauge is XLEN, not pending work; this increment documents that limitation and
does not invent a queue-backlog alarm. General model counters already contain judge traffic.
Observed costs remain telemetry; the durable workspace ledger remains authoritative.

## Validation

Promtool executes scenario fixtures, including recovery, counter reset, no traffic, replica
weighting and dynamically changed SLO targets. The normal Compose smoke starts both monitoring
containers, checks real authenticated scrapes, loaded rules and Grafana provisioning, and verifies
anonymous users cannot read the dashboard. No provider call is needed by these checks.

## Boundaries

The overlay is a local operator tool, not a production deployment or a tenant dashboard. An
external stack is still required for Kubernetes discovery, OTLP/log collection, retention policy,
monitoring of Prometheus itself and notification delivery. Alert rules do not deliver messages
without a configured receiver. Counter history is not durable evidence of end-to-end SLO compliance.

Configuration, launch steps, metric semantics and response runbooks are in
[the monitoring guide](../../infra/monitoring/README.md).

Skills: observability-otel, docker-kubernetes, security-threat-modeling, testing-quality,
cicd-release, code-review-debugging.
