# Going live: what an operator must supply

Everything in this repository is code, configuration and tests. This page lists the decisions,
accounts, credentials and infrastructure that cannot live in a repository and must be provided by
the organisation running Nexora. Work top to bottom: each section states what to do, where the
value is read, and how to check it took effect.

Nothing here is a default you can skip. Until a section is done, the capability it covers is off:
the platform fails closed rather than guessing.

## 1. Identity provider

Nexora authenticates people through your OIDC provider and never stores a password.

1. Register a **confidential web client**: authorization code flow with S256 PKCE and
   `client_secret_post` authentication.
2. Register the callback exactly: `https://<your-web-origin>/auth/callback` (and
   `http://localhost:3000/auth/callback` for local work).
3. Create a dedicated **API audience** for Nexora access tokens, separate from ID tokens and from
   any machine credential. Configure the provider to issue RS256 access tokens for it; the
   authorization request sends the `audience` parameter, so providers that use audience mappers
   need that mapping configured.
4. Decide who may sign in at all — group or application assignment in the provider. Nexora
   authorizes workspace access, not organisation membership.

Read as `NEXORA_AUTH_ISSUER`, `NEXORA_AUTH_AUDIENCE`, `NEXORA_OIDC_CLIENT_ID`,
`NEXORA_OIDC_CLIENT_SECRET`. Signing keys are discovered and rotated automatically; set
`NEXORA_AUTH_JWKS_URL` only for a deliberately cross-origin key endpoint.

Check: `/login` offers sign-in, and `GET /api/v1/me` with a provider access token returns your
issuer and subject.

## 2. Secrets

Generate these yourself and store them in your secret manager. None belongs in git, and the
Kubernetes Secret is created out of band (see `infra/k8s/README.md`).

| Secret | Used by | Requirement |
| --- | --- | --- |
| Database passwords (migration, API, worker logins) | all | three separate logins, per ADR 0031 |
| `NEXORA_REDIS_URL`, `NEXORA_SESSION_REDIS_URL` | API, worker, web | separate logical databases |
| `NEXORA_OIDC_CLIENT_SECRET` | web | from your provider |
| `NEXORA_METRICS_TOKEN` | API, worker, Prometheus | at least 32 characters |
| `NEXORA_OPENAI_API_KEY` | worker only | the API process never gets provider egress |
| `NEXORA_SPEND_ALERT_SIGNING_SECRET` | worker | at least 32 characters, only if you enable alert webhooks |
| `NEXORA_GRAFANA_ADMIN_PASSWORD` | monitoring stack | only if you run the bundled stack |

Decide the rotation interval for each and who holds it. Rotating the provider key or the metrics
token is a restart, not a migration.

## 3. Data and cache infrastructure

1. PostgreSQL 16 or later **with the `vector` extension available**. Provision it, then run
   `infra/postgres/001_extensions.sql` once per database.
2. Create the three database roles and logins described in `infra/k8s/README.md`, using
   `infra/postgres/production_roles.sql`. The migration identity must own the schema; the API and
   worker logins must not.
3. Redis for the job stream and browser sessions. Persistence matters: an outbox job is durable in
   PostgreSQL, but queue delivery state is not.
4. Backups and retention: decide the PITR window for PostgreSQL, and how long run events, spend
   ledger rows and audit rows are kept. Ledger, budget-event and run-event tables reject update
   and delete at the database, so retention means partition or archive policy, not `DELETE`.
5. Run the migration job before every rollout: `python -m nexora_api.migrate`, or the
   `infra/k8s/migrate` Job.

Check: `GET /api/v1/health/ready` returns 200; it requires PostgreSQL, pgvector and Redis.

## 4. Model provider and prices

1. Choose the provider account and model. Only the OpenAI Responses adapter ships today; the
   worker refuses a provider it does not have an adapter for.
2. Put the key in the worker environment only.
3. Decide the accounting prices for **every** candidate model and, if retrieval is on, the
   embedding model: `input_micros_per_million_tokens` and `output_micros_per_million_tokens`
   in the worker runtime file. A half-priced configuration stops the worker from starting, and an
   unpriced but routed model fails the run rather than running unmetered. These are your accounting
   rates, not an invoice from the provider — keep them updated when the provider's pricing changes.
4. Review the provider's data-processing terms against what your tenants will send. Nexora
   restricts egress to the configured endpoint; it cannot make a provider contract compliant.

Check: a run finishes, and `GET /api/v1/workspaces/{id}/spend` shows the priced call.

## 5. Worker runtime configuration

Copy `infra/worker/runtime.example.json`, edit it, and point `NEXORA_WORKER_RUNTIME_CONFIG` at it.
This file is the operator's control surface; a workspace can never change it. Decide:

- **Model profiles**: which workspaces may run, which providers and models they may use, which
  tools they may call, and the step limit. A run whose workspace is not listed fails before any
  provider call.
- **Retrieval**: leave it out to keep retrieval off. When on, choose the embedding model,
  dimensions, strategy (`hybrid` or `vector`), result limit and embedding price. If you configure
  the `ann` block, provision the matching HNSW index first with the migration credential.
- **Evaluation judge**: leave it out unless you want LLM judging, and pin the prompt version.
- **MCP servers**: each entry maps a `server_key` to a real HTTPS endpoint and the *name* of the
  environment variable holding its token. Workspaces reference the key only.
- **Spend alert webhook**: the HTTPS endpoint that receives threshold alerts and the name of the
  signing secret variable. Declaring it makes the worker refuse to start until the secret exists.

Check: the worker's readiness endpoint on port 8001 returns 200, and a queued run moves to
`running`. Without a worker, runs stay queued forever — that is the intended fail-closed state.

## 6. Network, TLS and the browser origin

1. DNS and TLS certificates for the web origin and the API origin.
2. `NEXORA_WEB_ORIGIN` must be the exact origin browsers use; the CSRF and OIDC callback checks
   compare against it.
3. Terminate TLS in front of the services. The containers serve plain HTTP inside the cluster.
4. Egress policy: the worker needs outbound HTTPS to your model provider and to any MCP server you
   allowlist. The API needs outbound HTTPS to the OIDC issuer only. The base NetworkPolicy is
   default-deny; widen it deliberately.

## 7. Delivery pipeline

1. Decide the container registry and who may push. The release workflow publishes by commit SHA
   with provenance and SBOM attestations; there is no `latest` tag.
2. Protect `main`: require the Platform CI checks and review. Promotion is a reviewed digest edit
   in `infra/k8s/overlays/production`.
3. Create the GitHub environment for staging and set:
   - variables: `NEXORA_STAGING_API_URL`, `NEXORA_STAGING_WORKER_ADMIN_URL`,
     `NEXORA_STAGING_WORKSPACE_ID`, `NEXORA_STAGING_AGENT_ID`, `NEXORA_STAGING_PROMPT`,
     `NEXORA_STAGING_EXPECT_TEXT`, and, when exercised, `NEXORA_STAGING_EXPECT_SOURCE_KEY`,
     `NEXORA_STAGING_APPROVAL_PROMPT`, `NEXORA_STAGING_APPROVAL_TOOL`,
     `NEXORA_STAGING_API_IMAGE`, `NEXORA_STAGING_WEB_IMAGE`;
   - secrets: `NEXORA_STAGING_ACCESS_TOKEN`, `NEXORA_STAGING_METRICS_TOKEN`, and
     `NEXORA_STAGING_KUBECONFIG` for the rollout drill.
4. Decide who may trigger the rollout/rollback drill and how often it runs. It intentionally
   leaves staging on the previous release.
5. For the **Deploy Staging** workflow, also set `NEXORA_STAGING_API_IMAGE` and
   `NEXORA_STAGING_WEB_IMAGE` to the registry paths CI publishes, and
   `NEXORA_STAGING_NAMESPACE` if the namespace is not `nexora-staging`. The staging Kubernetes
   identity needs get/patch/create on deployments and get/create/delete on jobs in that namespace,
   and nothing else. Before the first deploy, create the `nexora-secrets` Secret and the three
   database logins in the staging cluster exactly as section 2 and 3 describe — the deploy applies
   manifests, it does not create credentials.

The staging overlays pin the release they deploy. Promote them the same way production is
promoted, and merge that edit before dispatching the deploy:

```bash
python scripts/promote_release.py \
  --overlay infra/k8s/overlays/staging/kustomization.yaml \
  --image nexora/api --new-name <registry>/nexora-api --digest sha256:<digest>
python scripts/promote_release.py \
  --overlay infra/k8s/overlays/staging/migrate/kustomization.yaml \
  --image nexora/api --new-name <registry>/nexora-api --digest sha256:<digest>
```

## 8. Observability and on-call

1. Point `NEXORA_OTEL_EXPORTER_ENDPOINT` at your collector and choose
   `NEXORA_OTEL_SAMPLE_RATIO`. Unset means no span leaves the process.
2. Scrape `/metrics` on the API and worker with the metrics token. `infra/monitoring` has a
   Prometheus/Grafana stack and tested alert rules, but **notification routing is yours**:
   Alertmanager receivers, escalation, and who answers at 03:00.
3. Decide where structured JSON logs are shipped and how long they are kept. Prompts, tool
   arguments, tool results and retrieved context are deliberately never in telemetry; if you need
   that content for debugging, it stays in PostgreSQL under the same access rules as the console.
4. The declared SLOs — 99.9% API availability and 99% run reliability over 30 days — are
   engineering targets in code. Accept them or change them before you publish any commitment.

## 9. First workspace and the humans in the loop

1. Sign in; the first person to create a workspace becomes its owner.
2. Add members with `PUT /api/v1/workspaces/{id}/members` or the team access form, using the
   account identifiers your provider issues. This changes access directly — it sends no invitation.
3. Register tool contracts through the API for every tool an agent may call, then set each policy.
   A tool without a policy is denied.
4. Name the people who decide approvals and tell them where: destructive and outbound calls wait
   on the approvals page and expire if nobody answers within the approval window.
5. Set a monthly budget and alert thresholds per workspace. Without a limit, spend is metered but
   unlimited.
6. Write your own agent instructions and knowledge sources. Nexora ships no content.

## 10. Legal, policy and review

These are organisational decisions the platform records but cannot make:

- Which data may be sent to a model provider, and under which contract.
- Tenant data retention and deletion commitments, including run events and evaluation evidence.
- Who may read another person's run output. The platform's answer is "nobody but the requester";
  if your organisation needs a lawful-access path, design it deliberately rather than by widening
  the result endpoint.
- Acceptance of the residual risks in the threat model: a compromised MCP endpoint, a provider
  outage, and prompt injection in retrieved content, which is delimited and untrusted but not
  eliminated.

## Quick verification pass

Run these in order once the sections above are done:

```bash
curl -fsS https://<api-origin>/api/v1/health/ready
curl -fsS -H "Authorization: Bearer $TOKEN" https://<api-origin>/api/v1/me
curl -fsS -H "Authorization: Bearer $METRICS_TOKEN" https://<api-origin>/metrics | head -1
```

Then, in the console: create a workspace, create an agent, start a run, and confirm it reaches
`succeeded` with a result. If it stays `queued`, the worker is not running or the workspace is not
in a model profile — both are section 5.
