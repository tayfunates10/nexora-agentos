# Nexora operations monitoring

This opt-in local stack scrapes API and worker processes into Prometheus and provisions a
protected Grafana dashboard. It does not start model calls, send notifications, collect raw
prompts, or provision a production cluster. Run it alongside the existing Compose worker.

## Start

Configure the API, database and worker as described in the root README first. Export a shared
`NEXORA_METRICS_TOKEN` of at least 32 characters and a strong, private
`NEXORA_GRAFANA_ADMIN_PASSWORD` in your shell. Both must be present in the environment used by
Compose. Use at least 16 characters for the Grafana password. Do not commit their values. The metrics token must match the API and worker setting;
Prometheus reads it from a mounted secret file. Grafana reads its password from a separate secret.
The preparation script writes only to the ignored `.monitoring-secrets` directory (host mode 0700).
Files are readable by the non-root container UIDs; other host users cannot traverse the parent.
The directory is excluded from Docker build contexts. It contains plaintext local secrets, not an
encrypted secret vault. Re-run preparation after rotation and recreate the relevant containers.

```bash
python scripts/prepare_monitoring_secrets.py
docker compose -f compose.yaml -f compose.monitoring.yaml --profile worker up -d --build --wait
python scripts/check_monitoring.py
```

- Grafana: <http://localhost:3001>, user `admin`, password from your environment. Open
  **Nexora → Nexora operations**. Anonymous access and public signup are disabled.
- Prometheus: <http://localhost:9090>. Both services bind to loopback only. Prometheus has no
  interactive authentication; keep this local operator interface private.
- Persistent volumes retain state. The 35-day/2-GB Prometheus limits apply whichever is reached
  first; high-volume deployments may retain less than 30 days. The dashboard is provisioned from
  version control, so changes belong in its JSON file rather than the Grafana UI.
- The initial Grafana admin password applies when its database is created. Rotate an existing
  password using Grafana administration; changing the environment alone does not reset it.

Stop without deleting data:

```bash
docker compose -f compose.yaml -f compose.monitoring.yaml --profile worker down
```

## Signals and meaning

The ten panels cover scrape health, API errors and p95 latency, terminal run errors, model calls,
tokens, observed spend rate, budget refusals, judge outcomes and retained stream entries.
Missing traffic stays missing; a lack of samples is never converted to 100% success.

API availability counts 5xx responses against all matched/unmatched API requests, excluding
health and metrics routes. 4xx responses count as served requests. Terminal run reliability uses
`failed_terminal / (succeeded + failed_terminal)`; retry attempts, human-approval waits and user
cancellations are not terminal failures in this calculation. Process-local counters can lose
observations during a crash. These signals are operational evidence, not a durable run audit or
proof that a contractual 30-day SLO was achieved.

Fast-burn alerts require both five-minute and one-hour error ratios to exceed the threshold for
two minutes. The threshold reads the exported objective and window and corresponds to 2% of the
window's error budget consumed in one hour (14.4 times normal burn for a 30-day window). Alerts
resolve after the short window recovers. A cold start has only the samples collected so far;
operators must consider observation coverage when interpreting it.

Spend rates use operator accounting units and process counters, not invoice totals. The workspace
ledger is authoritative. Judge calls already contribute to general model counters, so do not add
the two. `nexora_queue_depth` currently measures Redis **XLEN**, including acknowledged entries;
its panel says retained stream entries and uses `max`, not `sum`, across workers observing the
same stream. It must not be used as a pending-work alarm.

## Target down

`NexoraMetricsTargetDown` fires after two minutes of failed scrapes. Check the API/worker readiness
endpoint, service logs, container networking and matching scrape credentials. A 401 usually means
the token differs; 404 means metrics are disabled. Do not remove authentication to make it green.
A target accidentally removed from configuration is outside this `up == 0` rule; the smoke check
requires both configured jobs. Monitoring Prometheus itself needs an independent external check.

## Error budget burn

`NexoraApiErrorBudgetBurn` and `NexoraTerminalRunErrorBudgetBurn` indicate sustained failures.
Check recent releases, provider availability and bounded error codes in structured logs. Follow
request/run trace IDs in your configured trace backend. Use the deployment rollback procedure
if the failures correlate with a release; avoid replaying side-effecting tool work manually.

Rules are visible on Prometheus **Alerts**. There is no Alertmanager receiver in this increment,
so no email, Slack or webhook delivery is claimed. Adding a receiver requires operator-owned
routing and credentials. Workspace percentage alerts in the product console are separate from
these deployment-wide operational alerts.

## Spend alert delivery

`NexoraSpendAlertUndelivered` fires when the worker gives up on a budget threshold notification
after its attempt limit. The crossing itself is durable and still visible in the workspace spend
console: what failed is the operator receiver. Check the endpoint's availability and its response
codes in the worker's structured logs, where the last error code is recorded without the payload.
A 4xx or a redirect is abandoned immediately by design, so a misconfigured URL or a rejected
signature shows up here rather than as an endless retry. Dead-lettered notifications are not
replayed automatically.

The rule sums across worker replicas, so it reports one deployment-wide alert rather than one per
pod and does not name the process that gave up. Search every worker replica's logs for the
delivery, and query the counter by `instance` to narrow it down.

## Validation and production handoff

The CI monitoring job runs real `promtool check rules` and `promtool test rules`. Fixtures cover
healthy/absent/all-failure traffic, multi-replica request weighting, runtime SLO changes, counter
resets, health-probe exclusion, cancellation/retry exclusion, alert delay and recovery. Compose
smoke starts actual Prometheus/Grafana containers, verifies authenticated API/worker scrapes,
loaded rules, provisioned datasource/dashboard and anonymous denial.

For Kubernetes, import the rules and dashboard into the operator's existing monitoring stack;
configure service discovery to scrape **each pod**, not a load-balanced service. Keep the job
labels `nexora-api` and `nexora-worker`, isolate one deployment per rule evaluation (or adapt all
aggregations with an explicit cluster label), and mount scrape credentials through the secret
manager. Configure retention, access control, independent monitoring of the monitoring stack,
and notification delivery before production use. This local stack does not install Kubernetes
monitoring, an OTLP collector, log shipping or notification channels.

References: [Prometheus configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/),
[rule testing](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/),
[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/),
[Grafana secret files](https://grafana.com/docs/grafana/latest/setup-grafana/configure-docker/).
