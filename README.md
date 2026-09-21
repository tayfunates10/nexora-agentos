# Nexora AgentOS

Enterprise-grade platform for building, orchestrating, securing, evaluating, and observing autonomous AI agents.

## Multi-agent engineering skills

Nexora keeps one canonical skill source under `skills/`. Generated mirrors make the same engineering guidance available to Claude Code, Codex-compatible agents, Cursor, Gemini-compatible discovery, OpenCode, and Copilot-oriented workflows.

After changing a canonical skill, run:

```bash
python scripts/sync_skills.py
python scripts/sync_skills.py --check
```

CI rejects skill drift so every supported coding agent receives the same project rules.

## Development status

The platform provides a Next.js control plane, typed FastAPI APIs, PostgreSQL/pgvector and
Redis infrastructure. The API includes verified bearer identities, workspace RBAC, browser OIDC
sessions, workspace-scoped agent definitions, durable runs, append-only run events, idempotency
and a transactional PostgreSQL outbox. The provider-independent worker runtime adds Redis
Streams consumer groups, durable job receipts, leases, fencing, bounded retries, cancellation
and crash recovery. Tool governance now adds a typed MCP tool registry, default-deny policy
evaluation, idempotent call records and durable human approvals. Provider routing now has a
normalized adapter contract plus an OpenAI Responses API adapter with fixed egress, normalized
errors/streaming and cancellation. The RAG foundation now adds versioned tenant-scoped sources,
ACL-filtered pgvector retrieval, deterministic chunking and citation provenance. The worker can now
opt into fixed-egress OpenAI embeddings and permission-aware retrieval through operator configuration.
The knowledge API can queue durable text/Markdown ingestion jobs; embedding and indexing stay in the
worker so the API process never gains provider egress. Durable deterministic evaluations now add
versioned golden suites, baseline regression comparison and failed-case evidence without giving the
API model-provider egress. Imported real-run evaluations can now queue a worker-side pinned LLM judge
for task-completion, relevance and clarity scoring while keeping probabilistic quality separate from
deterministic pass/fail.
Provider spend is now metered per workspace: operator-declared prices are converted to an
append-only cost ledger inside the transaction that commits each model step, judge case or indexed
knowledge version, and per-workspace monthly budgets are enforced before any provider egress,
generation and embeddings alike. Budget thresholds record an append-only alert the first time a
period reaches them, and a transactional outbox delivers each one to an operator-declared,
signed webhook. The web console reports that spend per period and lets owners and admins set
the cap and its thresholds.
Observability now adds durable run traces, guarded Prometheus exposition and structured
logs. The durable model executor now runs as an opt-in worker service configured by
operator-managed model profiles. Governed tools can now use operator-allowlisted MCP
2026-07-28 Streamable HTTP servers over public TLS; remote transport remains opt-in.
See [architecture and roadmap](docs/architecture/0001-foundation.md),
[agent run architecture](docs/architecture/0004-agent-runs-outbox.md),
[worker architecture](docs/architecture/0005-worker-state-machine.md), and
[MCP tool governance](docs/architecture/0006-mcp-tool-governance.md), and
[OpenAI provider adapter](docs/architecture/0007-openai-responses-adapter.md), and
[RAG foundation](docs/architecture/0008-rag-foundation.md), and
[durable executor](docs/architecture/0009-durable-executor.md), and
[observability](docs/architecture/0010-observability.md), and
[worker service](docs/architecture/0011-worker-service.md), and
[Kubernetes deployment](docs/architecture/0012-kubernetes-deployment.md), and
[release pipeline](docs/architecture/0013-release-pipeline.md), and
[RAG embedding runtime](docs/architecture/0014-rag-embedding-runtime.md), and
[MCP Streamable HTTP transport](docs/architecture/0015-mcp-streamable-http.md), and
[durable knowledge ingestion](docs/architecture/0016-knowledge-ingestion.md), and
[durable evaluations](docs/architecture/0017-durable-evaluations.md), and
[LLM judge evaluations](docs/architecture/0022-llm-judge-evaluations.md), and
[judge observability](docs/architecture/0024-evaluation-judge-observability.md), and
[spend governance](docs/architecture/0025-spend-governance.md), and
[spend console](docs/architecture/0026-spend-console.md), and
[spend alerts](docs/architecture/0027-spend-alerts.md), and
[API rate limiting](docs/architecture/0029-api-rate-limiting.md), and
[OIDC JWKS rotation](docs/architecture/0030-oidc-jwks-rotation.md), and
[PostgreSQL role separation](docs/architecture/0031-postgres-role-separation.md), and
[spend alert delivery](docs/architecture/0032-spend-alert-delivery.md), and
[hybrid RAG retrieval](docs/architecture/0033-hybrid-retrieval.md), and
[retrieval evals and HNSW](docs/architecture/0034-retrieval-evals-hnsw.md), and
[release supply-chain verification](docs/architecture/0035-release-supply-chain-verification.md), and
[deployed staging E2E smoke](docs/architecture/0036-staging-e2e-smoke.md).

## Run locally with Docker Compose

Requires Docker Compose v2. Copy `.env.example` to `.env` and replace the password
with a URL-safe random value (letters and digits work without URL escaping).

```bash
cp .env.example .env
# Edit .env before starting.
docker compose up --build -d
docker compose ps
```

- Web: http://localhost:3000
- API documentation: http://localhost:8000/docs
- Liveness: http://localhost:8000/api/v1/health/live
- Readiness: http://localhost:8000/api/v1/health/ready

Readiness returns 503 if PostgreSQL, pgvector or Redis is unavailable. Liveness
remains independent of those services. Refresh the panel to re-check current health.
`docker compose down` stops services and preserves data volumes.
The init SQL enables pgvector only on a new database volume.

## Run application code without Docker

Requires Node.js 22 or 24, Python 3.12+, and running PostgreSQL/Redis for healthy readiness.

```bash
npm ci
python -m venv .venv
. .venv/bin/activate
pip install -r apps/api/requirements-dev.lock
pip install --no-deps -e apps/api
export NEXORA_DATABASE_URL='postgresql://nexora:YOUR_PASSWORD@localhost:5432/nexora'
export NEXORA_REDIS_URL='redis://localhost:6379/0'
uvicorn nexora_api.main:app --reload
```

In another terminal, run `npm run dev`. The server-side web client defaults to
`http://127.0.0.1:8000`; override with `NEXORA_API_URL` when required. The API reads
process environment variables; it does not automatically load the root Compose `.env`.
The panel still starts with an unavailable state if the API is offline.

## Deploying to Kubernetes

`infra/k8s` holds plain Kustomize manifests for the API, worker and web: restricted pod
security, default-deny networking, a migration Job applied before each rollout, and
digest-pinned images in the production overlay. No credential is committed; the Secret is
created out of band. See [infra/k8s/README.md](infra/k8s/README.md) for the apply and
rollback sequence and [ADR 0012](docs/architecture/0012-kubernetes-deployment.md) for the
boundaries these manifests enforce.

Pushing to `main` builds each image once, publishes it tagged by commit SHA with
BuildKit provenance and SBOM attestations, scans that immutable digest for HIGH/CRITICAL
vulnerabilities, then creates and verifies a GitHub/Sigstore signed provenance attestation.
Fixable HIGH/CRITICAL findings stop the release before a promotion command is emitted.
Promotion is an explicit digest edit, reviewed and merged like any other change; there is
no `latest` tag to drift. CI also refuses a change that modifies or deletes a migration a
deployed database has already applied, which is what makes `kubectl rollout undo` safe.
See [ADR 0013](docs/architecture/0013-release-pipeline.md) and
[ADR 0035](docs/architecture/0035-release-supply-chain-verification.md).

A manually triggered **Staging E2E Smoke** workflow validates a real deployed environment without
placing provider credentials in GitHub Actions. It uses a short-lived API access token and a
pre-provisioned staging fixture to prove workspace/agent access, idempotent durable execution,
real worker/model activity, requester-scoped results and run events. Retrieval is verified by a
worker metric delta; optional approval mode approves a configured staging tool and requires the
worker to resume the run. See [ADR 0036](docs/architecture/0036-staging-e2e-smoke.md).

## Quality checks

```bash
python scripts/sync_skills.py --check
python -m unittest discover -s scripts -p 'test_staging_smoke.py'
.venv/bin/ruff check apps/api
.venv/bin/ruff format --check apps/api
.venv/bin/pytest apps/api/tests -m 'not integration'
npm run typecheck
npm run test:web
npm run build
```

For real dependency integration, start PostgreSQL/Redis, export the connection variables
above, then run `NEXORA_INTEGRATION=1 .venv/bin/pytest apps/api/tests -m integration`.
The CI workflow also runs this test against service containers.

## Identity and workspace API

See [identity architecture](docs/architecture/0002-identity-workspaces.md) for boundaries
and known limitations. The API validates RS256 access tokens from a configured issuer.
Set `NEXORA_AUTH_ISSUER` and `NEXORA_AUTH_AUDIENCE`. By default the API retrieves the
issuer's OIDC discovery document, caches its JWKS signing keys, and refreshes on an unseen
`kid` so normal provider key rotation needs no restart. A discovered cross-origin
`jwks_uri` is refused unless the operator explicitly sets `NEXORA_AUTH_JWKS_URL`.
`NEXORA_AUTH_PUBLIC_KEY` remains available only when a deployment deliberately wants to
pin one RSA public PEM. Token-supplied key URLs are never followed. Use a dedicated audience
for Nexora user access tokens, distinct from ID tokens and machine/service credentials.
Without issuer/audience configuration authenticated endpoints fail closed. Health endpoints
remain public.

Compose runs a one-shot migration service before the API. For a non-Docker setup:

```bash
.venv/bin/python -m nexora_api.migrate
```

Run this before starting the upgraded API. Migration state is checksum-verified and
repeated runs are safe; no existing volumes need deletion.

With a valid provider-issued access token in `$NEXORA_ACCESS_TOKEN`:

```bash
curl -H "Authorization: Bearer $NEXORA_ACCESS_TOKEN" http://localhost:8000/api/v1/me
curl -X POST http://localhost:8000/api/v1/workspaces \
  -H "Authorization: Bearer $NEXORA_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"My workspace"}'
```

Use `/docs` for the full API schema. Owners can assign `admin` or `member` to a provider
subject with `PUT /api/v1/workspaces/{id}/members`. Admins can rename a workspace;
members can only read. The initial owner cannot be demoted, and assigning a second
owner is blocked. This endpoint changes database membership; it does not send invitations.
The web panel includes browser sign-in and workspace management (setup below).
Authenticated API rate limiting is shared across replicas through Redis, API bearer
verification rotates provider JWKS signing keys automatically, and the production Kubernetes
path now separates migration, API and worker PostgreSQL identities with reviewed runtime grants.

## Agent definitions, durable runs and worker orchestration

Owners and admins can create agent definitions. Any current workspace member can start a run.
Run creation requires an `Idempotency-Key`; replaying the same request returns the existing run
instead of duplicating work. The initial run, append-only event, security audit and outbox job
are committed atomically.

The outbox publisher sends identifier-only jobs to the Redis Stream
`nexora:jobs:agent-runs:v1`. Prompt/input content remains in PostgreSQL and is not copied into
the queue. The worker library uses consumer-group delivery, durable job receipts, bounded leases,
heartbeats, fencing, bounded retries with jitter, cancellation and stale-run recovery. A worker
that has lost its lease cannot finalize a run.

The durable model executor runs behind operator-managed model profiles
(see [ADR 0009](docs/architecture/0009-durable-executor.md)); there is no autonomous loop.
Governed tool execution is available behind an MCP adapter boundary, but no arbitrary URL/stdio
transport is enabled by default. The worker runs as an opt-in Compose profile (below); it is
never started by the default stack.

## Running the agent worker

The worker executes queued runs. It refuses to start without operator configuration, so
nothing runs unless a deployment explicitly allows it.

1. Copy `infra/worker/runtime.example.json`, set the workspace IDs, provider and model your
   deployment allows, and point `NEXORA_WORKER_RUNTIME_CONFIG` at it.
2. Put the provider credential in the environment (`NEXORA_OPENAI_API_KEY`), never in the
   configuration file — an unknown key there is rejected. The same holds for the spend alert
   signing secret, which the configuration names but never contains.
3. Start it: `docker compose --profile worker up --build -d`, or run
   `python -m nexora_api.worker_main` with the same variables exported.

```json
{
  "model_candidates": [
    {
      "provider": "openai",
      "model": "your-model",
      "capabilities": ["text", "tools", "structured_output"],
      "input_micros_per_million_tokens": 3000000,
      "output_micros_per_million_tokens": 15000000
    }
  ],
  "profiles": {
    "default": {
      "allowed_workspaces": ["<workspace-uuid>"],
      "allowed_providers": ["openai"],
      "allowed_tools": [],
      "max_steps": 8
    }
  },
  "mcp_servers": {},
  "retrieval": {
    "provider": "openai",
    "model": "text-embedding-3-small",
    "dimensions": 1536,
    "strategy": "hybrid",
    "ann": {
      "ef_search": 100
    },
    "limit": 8,
    "input_micros_per_million_tokens": 20000
  },
  "evaluation_judge": {
    "provider": "openai",
    "model": "your-model",
    "allowed_workspaces": ["<workspace-uuid>"],
    "prompt_version": "nexora-eval-judge-v1"
  },
  "spend_alert_webhook": {
    "url": "https://alerts.your-operator.example/hooks/nexora-spend",
    "signing_secret_env": "NEXORA_SPEND_ALERT_SIGNING_SECRET"
  }
}
```

`spend_alert_webhook` is shown here for its shape and is deliberately absent from
`infra/worker/runtime.example.json`: declaring it makes the worker refuse to start until the
named signing secret is in the environment, which is the right behaviour for an operator who
chose an endpoint and the wrong default for a copy of the example.

A run selects a profile only through the `model_profile` on its agent definition; it cannot
name a model, provider, endpoint or tool the profile does not list. A run whose workspace is
not in the profile fails with `model_profile_not_authorized` without reaching a provider.

The worker serves liveness, readiness and token-guarded metrics on port 8001
(`NEXORA_WORKER_ADMIN_PORT`), using the same scrape token as the API. SIGTERM stops it between
jobs so an in-flight attempt finishes under its own lease. Retrieval remains off unless the
operator supplies a `retrieval` block. When enabled, the worker checks current workspace
membership before embedding the query, applies source ACLs in SQL, and injects only delimited
untrusted evidence with citation provenance. Retrieval defaults to `hybrid`: vector and
language-neutral PostgreSQL full-text candidates are fused with deterministic RRF so exact
identifiers can recover from a weak embedding match. Operators can set `strategy: "vector"`
to retain vector-only ranking.

ANN is optional. Before adding an `ann` block, provision the exact model/dimension HNSW index
with the migration-owner database credential:

```bash
python -m nexora_api.rag_ann ensure --model text-embedding-3-small --dimensions 1536
```

With `ann.ef_search` configured, the worker verifies that index before calling the embedding
provider, enables filtered iterative HNSW scans, and fails closed instead of silently falling back
to a full scan. Retrieval quality can be evaluated independently with the deterministic
`nexora_api.retrieval_eval` hit@K, recall@K and MRR helpers. See
[ADR 0034](docs/architecture/0034-retrieval-evals-hnsw.md), in addition to
[ADR 0011](docs/architecture/0011-worker-service.md) and
[ADR 0014](docs/architecture/0014-rag-embedding-runtime.md).

Use `/docs` for the full schema. Core endpoints are:

- `POST /api/v1/workspaces/{workspace_id}/agents`
- `GET /api/v1/workspaces/{workspace_id}/agents`
- `POST /api/v1/workspaces/{workspace_id}/runs`
- `GET /api/v1/workspaces/{workspace_id}/runs/{run_id}`
- `GET /api/v1/workspaces/{workspace_id}/runs/{run_id}/result` (original requester only)
- `POST /api/v1/workspaces/{workspace_id}/runs/{run_id}/cancel`
- `GET /api/v1/workspaces/{workspace_id}/runs/{run_id}/events`

See [ADR 0004](docs/architecture/0004-agent-runs-outbox.md) for persistence/outbox semantics and
[ADR 0005](docs/architecture/0005-worker-state-machine.md) for worker state transitions,
at-least-once delivery, leases, retry, cancellation and recovery.

The result endpoint requires current workspace membership and the original requester identity;
owner/admin status does not grant access to another user's raw answer. Successful runs return the
final text and explicit stop/refusal reason. Failed/cancelled runs return no partial answer.
Nonterminal runs and incomplete success journals return HTTP 409.

Results also include model-step metadata, recorded token totals and model-selected tool names.
Token totals cover persisted responses only; tool selection does not imply execution or approval.
Arguments, intermediate text and tool outputs are omitted. This endpoint does not automatically
create evaluations. See [ADR 0019](docs/architecture/0019-agent-run-results.md).

## Governed MCP tools and approvals

Owners and admins can register workspace tool contracts and set an explicit policy. Missing policy
means deny. Destructive and external-communication tools require approval even when their stored
policy says allow. Non-read tool schemas must require an idempotency key.

Core endpoints are:

- `PUT /api/v1/workspaces/{workspace_id}/tools/{tool_name}`
- `GET /api/v1/workspaces/{workspace_id}/tools`
- `PUT /api/v1/workspaces/{workspace_id}/tools/{tool_name}/policy`
- `GET /api/v1/workspaces/{workspace_id}/approvals`
- `POST /api/v1/workspaces/{workspace_id}/approvals/{approval_id}/decision`

An agent executor calls the internal `McpGateway` with a stable per-run call key. The gateway
validates arguments, rechecks requester membership and current policy, persists the call and either
executes through an operator-provided `server_key` adapter or raises a durable approval request.
The worker releases its lease while waiting. Approval requeues the run through the transactional
outbox; rejection, expiry or run cancellation closes the workflow without calling the adapter.

Workspace users cannot register raw MCP URLs, stdio commands or credentials. This keeps network
egress and service credentials outside model-visible configuration. Operators may opt into a
remote server in the worker runtime file:

```json
{
  "mcp_servers": {
    "ops": {
      "transport": "streamable_http",
      "url": "https://mcp.example.com/mcp",
      "bearer_token_env": "NEXORA_MCP_OPS_TOKEN",
      "timeout_seconds": 15,
      "max_response_bytes": 131072
    }
  }
}
```

The token value stays in the process environment; only its environment-variable name is in the
runtime file. A workspace tool may reference the bounded `server_key` (`ops` above), but cannot
change that server's URL or credentials. The built-in adapter pins MCP revision `2026-07-28`,
sends the required method/name headers, accepts JSON or SSE responses, refuses redirects and
private/local literal endpoints, and bounds response bytes. It currently supports synchronous
`tools/call` results only: MRTR `input_required`, Tasks results and non-text fallback content
fail closed rather than widening the trust boundary.

See [ADR 0006](docs/architecture/0006-mcp-tool-governance.md) and
[ADR 0015](docs/architecture/0015-mcp-streamable-http.md).

## Knowledge ingestion API

Owners and admins can queue text or Markdown knowledge sources without giving the API process
provider network access. Ingestion is durable and idempotent: the API stores validated source
content and ACL metadata in PostgreSQL, then a retrieval-enabled worker embeds and indexes it
through the operator-configured embedding adapter. Raw source text is never copied to Redis.

Core endpoints are:

- `POST /api/v1/workspaces/{workspace_id}/knowledge/sources`
- `GET /api/v1/workspaces/{workspace_id}/knowledge/ingestions/{job_id}`
- `GET /api/v1/workspaces/{workspace_id}/knowledge/sources`
- `DELETE /api/v1/workspaces/{workspace_id}/knowledge/sources/{source_key}`

Create requests require an `Idempotency-Key`. Workspace-wide sources are visible to current
workspace members; restricted sources are omitted from listing unless the current issuer/subject
appears in that source's ACL. Worker execution rechecks `knowledge:manage` before any paid
embedding call, so a user whose role was revoked after enqueue cannot trigger provider egress.

Deletion cancels queued ingestion for the same source key. A source with a currently running
ingestion returns HTTP 409 rather than racing the worker and being recreated after deletion.
The first public ingestion surface accepts bounded UTF-8 text/Markdown; PDF parsing and other
untrusted binary formats remain a separate parser/sandbox milestone. See
[ADR 0016](docs/architecture/0016-knowledge-ingestion.md).

## Durable deterministic evaluations

The workspace panel links to **Evaluation suites**. Members can inspect versioned case
definitions; owners and admins can also browse newest-first result history, open per-case
failures and retained output, and follow the saved baseline comparison. Empty and unavailable
states are shown explicitly. This read-only console does not run models automatically.
See [ADR 0018](docs/architecture/0018-evaluation-history-console.md).

Owners and admins can create immutable, versioned golden evaluation suites and score candidate
observations without calling a model provider from the API process. Each run must submit exactly one
observation for every case, so pass/fail rates remain comparable.

Core endpoints are:

- `POST /api/v1/workspaces/{workspace_id}/eval-suites`
- `GET /api/v1/workspaces/{workspace_id}/eval-suites`
- `GET /api/v1/workspaces/{workspace_id}/eval-suites/{suite_id}`
- `POST /api/v1/workspaces/{workspace_id}/eval-suites/{suite_id}/runs`
- `POST /api/v1/workspaces/{workspace_id}/eval-suites/{suite_id}/run-imports`
- `GET /api/v1/workspaces/{workspace_id}/eval-suites/{suite_id}/runs` (owner/admin history)
- `GET /api/v1/workspaces/{workspace_id}/eval-runs/{eval_run_id}`

Creating suites or runs requires an `Idempotency-Key`. A baseline must use the exact same suite
version. A case is marked as a regression only when the baseline passed and the candidate fails; the
inverse is recorded as an improvement. Raw output is retained only for failed cases and ordinary
run details require owner/admin evaluation-management permission. Imported run details remain
scoped to the importing requester because their failed raw output can contain requester-specific
retrieval data; other owners/admins can still see the non-sensitive history summary.

Agent-run imports map exactly one requester-owned successful agent run to each suite case. The
persisted case input must match the agent-run input exactly; selected tools and final output are
derived from the immutable model journal, not supplied by the client. Source run IDs are preserved as
append-only evaluation provenance. Retrieval-enabled runs now persist immutable source/version/chunk
identifiers and hashes before model generation; exact retrieved context is kept only while a run may
retry or resume and is deleted on terminal status. Replay revalidates current chunk identity and ACLs
instead of silently re-running retrieval. Automated imports derive canonical citations such as
`rag:handbook@v1` only from that durable provenance; model-authored citation-looking text is never
accepted as proof. Older runs without provenance still fail closed when a suite requires citations.
See [ADR 0020](docs/architecture/0020-agent-run-evaluation-import.md) and
[ADR 0021](docs/architecture/0021-run-retrieval-provenance.md).

History accepts `limit` (1–100, default 25) and the `next_cursor` returned by the preceding
page. It orders runs newest first and excludes raw output and case details. Cursors are scoped
to the selected workspace and suite; an unknown or unrelated cursor returns 404.

The first layer remains deterministic: expected tools, forbidden tools and required citation
identifiers. For agent-run imports, owners/admins can additionally queue an asynchronous pinned judge
with `POST /api/v1/workspaces/{workspace_id}/eval-runs/{eval_run_id}/judge-runs` and read it with
`GET /api/v1/workspaces/{workspace_id}/eval-judge-runs/{judge_run_id}`. Judge v1 scores task
completion, relevance and clarity from 0-4, with a deterministic 0-1000 aggregate. When the eval run
has a baseline, candidate and baseline are scored by the same pinned provider/model/prompt and a
per-case quality delta is stored. Judge results never alter deterministic pass/fail. See
[ADR 0017](docs/architecture/0017-durable-evaluations.md),
[ADR 0022](docs/architecture/0022-llm-judge-evaluations.md), and
[ADR 0023](docs/architecture/0023-evaluation-judge-console.md).

## Spend accounting and budgets

The workspace panel links to **Spend and budget**. Members can read the current UTC month: consumed
and remaining amounts, the budget state, the per-category breakdown and the priced-call ledger with
cursor pagination and a category filter across agent runs, quality judging and embeddings, plus the
thresholds reached this period. Owners and admins can also set the monthly limit, enforcement mode
and alert thresholds through a CSRF-protected form; the browser never receives the API token. Amounts
are converted between units and exact micros with integer arithmetic, and an amount that cannot be
represented exactly is refused by both the input pattern and the server route. See
[ADR 0026](docs/architecture/0026-spend-console.md).

Provider spend is metered per workspace. Operators declare a price for every model candidate in the
worker runtime configuration (`input_micros_per_million_tokens` and
`output_micros_per_million_tokens`) and, when retrieval is enabled, an input rate for the embedding
model; a run, an agent or a tenant can never influence one. Cost is an integer count of micros —
millionths of one unit of the operator accounting currency — and a partial price unit always rounds
up.

A deployment prices every candidate or none, and prices retrieval embeddings exactly when it prices
candidates. Leaving prices out keeps accounting and enforcement off instead of recording a fabricated
zero cost; a half-priced configuration stops the worker from starting, and a model that is routed but
unpriced fails the run with `model_price_not_configured` rather than executing unmetered.

Each priced call is written to an append-only ledger in the same transaction that commits the work it
pays for — the agent model step, the judge case score, or the indexed knowledge version — under a
deterministic source key, so a retried, resumed or crash-recovered attempt is never charged twice. A
query embedding has no durable artifact of its own, so it is recorded as soon as the provider answers,
under its run's retrieval key; a retrieval that cannot name a key to charge is refused before egress.

- `GET /api/v1/workspaces/{workspace_id}/spend` — current UTC month: consumed micros, limit,
  enforcement, remaining micros and per-category totals (members and above).
- `PUT /api/v1/workspaces/{workspace_id}/spend/budget` — set `monthly_limit_micros`,
  `enforcement` (`enforce` or `monitor`) and up to five `alert_thresholds` percentages (default
  80 and 100, empty list to disable); owners and admins only.
- `GET /api/v1/workspaces/{workspace_id}/spend/records` — the priced calls themselves, newest first,
  with `limit` (1–100, default 25), `next_cursor` and an optional `category` filter.

The worker checks the remaining budget before provider egress. In `enforce` mode an exhausted budget
stops the run terminally with `workspace_budget_exhausted` and appends a `spend.denied` run event
carrying the limit and consumed amount; a judge job fails with `judge_budget_exhausted` and a
knowledge ingestion job fails terminally with `workspace_budget_exhausted`, both before any request
leaves the process. In `monitor` mode the overage is reported and execution continues. The
check is a gate rather than a hard cap: the call that crosses the limit is the last one allowed, and
concurrent runs can overshoot by the cost of the calls already in flight, bounded by the profile
token limits. A workspace without a budget is metered but unlimited.

Alert thresholds warn before the backstop. The first time a period's spend reaches a configured
percentage of the limit, an append-only alert row records the threshold with the limit, consumed
amount and enforcement mode as they stood at that moment; a unique key per workspace, period and
threshold is what keeps a busy month from repeating one warning. Crossing is compared with integer
arithmetic, and writing a tighter budget re-evaluates thresholds because a lower limit can cross one
without new spend. Alerts never block a call — enforcement does that. See
[ADR 0027](docs/architecture/0027-spend-alerts.md).

Each alert also queues one notification in the same statement that records it, so a warning is
never owed for a crossing that rolled back and a crossing never commits without its notification
queued. Delivery is opt-in worker work: declare `spend_alert_webhook` in the runtime configuration
with an HTTPS endpoint on port 443 and the name of the environment variable holding a signing
secret of at least 32 characters. A workspace can never name that URL. Requests carry
`spend.threshold.reached.v1` with identifiers and amounts only — no workspace name, user, prompt,
model or retrieval data — plus `nexora-timestamp` and an HMAC-SHA256 `nexora-signature` a receiver
must verify over the exact bytes before parsing, and `nexora-delivery-id` to deduplicate the
at-least-once retries. Redirects are refused, responses are drained under a bound and never parsed,
retryable failures back off, and a notification is dead-lettered after five attempts rather than
retried forever. Without a configured endpoint notifications simply stay queued. See
[ADR 0032](docs/architecture/0032-spend-alert-delivery.md).

Budget changes are recorded twice — in an append-only budget event with actor and request identity,
and in the workspace security audit trail. Ledger rows and budget events reject update, delete and
truncate at the database. The ledger covers every provider call the platform makes for a tenant —
agent runs, quality judging and both embedding paths — priced with operator rates; it is not a
provider invoice. See [ADR 0025](docs/architecture/0025-spend-governance.md).

## Traces, metrics and structured logs

Every run carries a durable `trace_id`, and that UUID is used directly as the
OpenTelemetry trace id. API, worker, retrieval, model and tool spans therefore join one
trace across processes, retries and approval resumes, without putting trace context into
the job queue. Spans and logs record identifiers, counts, policy actions and error codes
through an allowlist; prompts, tool arguments, tool results and retrieved context are
never sent to telemetry.

Telemetry is operator-owned and off by default:

- `NEXORA_OTEL_EXPORTER_ENDPOINT` — OTLP/HTTP collector URL. Unset means no span leaves
  the process.
- `NEXORA_OTEL_SAMPLE_RATIO` — head sampling between 0.0 and 1.0 (default 1.0). The
  decision is derived from the run trace id, so every process agrees on it.
- `NEXORA_METRICS_TOKEN` — scrape credential of at least 32 characters. Unset means
  `/metrics` returns 404; a wrong token returns 401.
- `NEXORA_LOG_LEVEL` — level for the JSON stdout logger (default `INFO`).

Scrape the API with the operator token:

```bash
curl -H "Authorization: Bearer $NEXORA_METRICS_TOKEN" http://localhost:8000/metrics
```

Exposed series include `nexora_http_requests_total`,
`nexora_http_request_duration_seconds`, `nexora_agent_runs_total`,
`nexora_agent_run_duration_seconds`, `nexora_model_calls_total`,
`nexora_model_tokens_total`, `nexora_tool_calls_total`,
`nexora_retrieval_queries_total`, `nexora_approval_wait_seconds`,
`nexora_queue_depth`, `nexora_outbox_published_total`,
`nexora_evaluation_judge_calls_total`,
`nexora_evaluation_judge_call_duration_seconds`,
`nexora_evaluation_judge_tokens_total`,
`nexora_evaluation_judge_jobs_total`,
`nexora_model_cost_micros_total`,
`nexora_spend_denials_total` and
`nexora_spend_alerts_total` and
`nexora_spend_alert_deliveries_total`.

Two user-facing objectives are declared in code and exported alongside them, so alert
rules read the stated goal rather than a hardcoded number: API availability at 99.9% and
agent run reliability at 99%, both over a rolling 30-day window
(`nexora_slo_objective_ratio`, `nexora_slo_window_days`). They are initial engineering
targets, not contractual guarantees.

Metric labels stay bounded deliberately: workspace, user, run, approval and tool names
are tenant data and remain on spans, while metrics carry only HTTP method, matched route
template, status, outcome, provider, MCP server key, token kind, approval decision and fixed
evaluation-judge target/outcome, spend-category and whole-percent threshold values.
Unmatched paths collapse to `unmatched` and unexpected label values to `other`, so no
request can grow the series count. Metrics are per-process, so each replica is scraped
separately. See [ADR 0010](docs/architecture/0010-observability.md) for trace identity,
sampling, egress boundaries and the starting SLOs.

### Operations dashboard and alert rules

An opt-in [Prometheus/Grafana stack](infra/monitoring/README.md) now includes authenticated
API/worker scraping, ten operator panels, target-down and SLO fast-burn rules, tested alert
scenarios and response runbooks. It binds to loopback and requires operator credentials.
The rules surface alerts in Prometheus; notification delivery and production monitoring
infrastructure remain operator setup. See [ADR 0028](docs/architecture/0028-operations-monitoring.md).

## Browser sign-in and workspace management

The web now includes `/login`, `/workspaces`, workspace settings and team access forms.
Provider registration is still required; without configuration `/login` explains that
sign-in is unavailable and protected pages redirect there.

1. Register a **confidential web client** at your OIDC provider. Enable authorization code
   flow with S256 PKCE and `client_secret_post` authentication.
2. Register exactly `http://localhost:3000/auth/callback` for local Compose (or your HTTPS
   origin plus `/auth/callback` for deployment). Use that same host in the browser.
3. Configure the provider to issue RS256 user access tokens for the dedicated Nexora API
   audience and ID tokens for the web client ID. The authorization request includes the
   `audience` parameter; providers that use audience mappers must configure them accordingly.
4. Set `NEXORA_WEB_ORIGIN`, `NEXORA_AUTH_ISSUER`, `NEXORA_AUTH_AUDIENCE`,
   `NEXORA_OIDC_CLIENT_ID`, and `NEXORA_OIDC_CLIENT_SECRET` privately in your environment.
   The API normally discovers and rotates the provider's JWKS automatically; only set
   `NEXORA_AUTH_JWKS_URL` for an intentionally cross-origin key endpoint or
   `NEXORA_AUTH_PUBLIC_KEY` when deliberately pinning one PEM. Do not paste secrets into
   source files or commit `.env`.
5. Compose forwards these settings and supplies the web session Redis URL. Outside Compose,
   set `NEXORA_SESSION_REDIS_URL=redis://127.0.0.1:6379/1` for the web process as well.
6. Restart services and visit `/login`. Create a workspace, rename it, or assign admin/member
   access using an organization account ID. Actual authorization is always checked by API.

Tokens remain server-side in Redis; browser cookies contain opaque IDs only. Sessions
expire after at most one hour or earlier with the access token. Sign-out revokes the
Nexora session immediately; it does not close the provider's own session. See
[browser-session architecture](docs/architecture/0003-browser-sessions.md).

Browser test (requires a local Redis and installed Chromium; no real provider credentials):

```bash
npm run build
npx playwright install chromium
npm run test:e2e --workspace @nexora/web
```

The browser test starts its own test-only issuer/API and a production web server on port
3100; Redis defaults to database 15. Test issuer/API code is never exposed in the app.
