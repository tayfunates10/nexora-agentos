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

The first application milestone provides a Next.js service-health panel, typed FastAPI
health endpoints, PostgreSQL/pgvector and Redis infrastructure. The API now includes verified bearer identities, workspace creation/listing/renaming,
and owner/admin/member authorization. Browser login, provider provisioning, agent
execution and MCP integration are **not implemented yet**.
See [architecture and roadmap](docs/architecture/0001-foundation.md).

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
The web panel still shows foundation health; browser sign-in and workspace management
UI are the next milestone. Rate limiting, automatic key rotation and production database
role separation remain deployment work before public exposure.
