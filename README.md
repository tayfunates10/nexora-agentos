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
errors/streaming and cancellation. A production model executor and production MCP transport
adapter are not enabled yet.
See [architecture and roadmap](docs/architecture/0001-foundation.md),
[agent run architecture](docs/architecture/0004-agent-runs-outbox.md),
[worker architecture](docs/architecture/0005-worker-state-machine.md), and
[MCP tool governance](docs/architecture/0006-mcp-tool-governance.md), and
[OpenAI provider adapter](docs/architecture/0007-openai-responses-adapter.md).

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

## Quality checks

```bash
python scripts/sync_skills.py --check
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
Set `NEXORA_AUTH_ISSUER`, `NEXORA_AUTH_AUDIENCE`, and `NEXORA_AUTH_PUBLIC_KEY` in the
API process environment. The public key must contain real PEM newlines; it is the
verification key from your identity provider, never a private signing key. For Compose,
these settings are forwarded from the shell or `.env` (which supports quoted multiline
values). Use a dedicated audience for Nexora user access tokens, distinct from ID tokens
and machine/service credentials. Without this configuration authenticated endpoints
fail closed. Health endpoints remain public.

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
Rate limiting, automatic key rotation and production database
role separation remain deployment work before public exposure.

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

The runtime still has no production model executor or autonomous loop. Governed tool execution
is available behind an MCP adapter boundary, but no arbitrary URL/stdio transport is enabled by
default and no worker service is added to Compose until production provider and MCP adapters are
configured.

Use `/docs` for the full schema. Core endpoints are:

- `POST /api/v1/workspaces/{workspace_id}/agents`
- `GET /api/v1/workspaces/{workspace_id}/agents`
- `POST /api/v1/workspaces/{workspace_id}/runs`
- `GET /api/v1/workspaces/{workspace_id}/runs/{run_id}`
- `POST /api/v1/workspaces/{workspace_id}/runs/{run_id}/cancel`
- `GET /api/v1/workspaces/{workspace_id}/runs/{run_id}/events`

See [ADR 0004](docs/architecture/0004-agent-runs-outbox.md) for persistence/outbox semantics and
[ADR 0005](docs/architecture/0005-worker-state-machine.md) for worker state transitions,
at-least-once delivery, leases, retry, cancellation and recovery.

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
egress and service credentials outside model-visible configuration. See
[ADR 0006](docs/architecture/0006-mcp-tool-governance.md).

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
   Configure the API's `NEXORA_AUTH_PUBLIC_KEY` with the provider's verification PEM as
   described above. Do not paste secrets into source files or commit `.env`.
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
