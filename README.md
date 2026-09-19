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
health endpoints, PostgreSQL/pgvector and Redis infrastructure. Agent execution,
authentication, workspace RBAC and MCP integration are **not implemented yet**.
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
