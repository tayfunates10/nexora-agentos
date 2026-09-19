# ADR 0001: A health-checked monorepo foundation

Status: accepted for the foundation milestone.

## Scope and assumptions
The repository previously contained engineering skills only. This increment establishes
Next.js/TypeScript in apps/web and FastAPI/Python in apps/api, with PostgreSQL/pgvector
and Redis in local Compose. No tenant data or execution endpoints exist yet. The web
reads actual API readiness on the server; unavailable services are never replaced by
sample metrics. Versions are pinned through package-lock.json and Python requirements locks.
Compose is for local development, with host ports bound to loopback.

## Boundaries
Web -> versioned API -> application services -> domain -> adapters/repositories.
The initial API only exposes health and OpenAPI. Database and Redis adapters implement
bounded checks. A Redis outage does not kill liveness; readiness returns 503.
Postgres readiness requires the vector extension. Health responses exclude connection
strings and driver errors. The web validates response data at runtime using Zod.
Request IDs are bounded, validated correlation values, never credentials or authorization.

## Subsequent milestones
1. Identity adapter, workspace membership, RBAC, migrations, tenant isolation tests.
2. Agent definitions and durable run/event storage; transactional job publishing.
3. Worker state machine with explicit retries, cancellation and recovery.
4. MCP gateway with typed tool registry, policy checks and durable human approvals.
5. Provider adapters, RAG, evaluations, OpenTelemetry and deployment hardening.

The runtime and gateway will be separate workers/services when implemented. Do not
add placeholder processes that report healthy without doing work. No model API keys
are required for this increment. No LLM calls or privileged tools are enabled.

## Runtime design constraints for the next milestone
Runs must carry workspace_id, user_id, agent_id, run_id and trace_id. Planned transitions:
queued -> running -> succeeded/failed/cancelled; running -> waiting_for_approval;
waiting_for_approval -> queued (approved) or cancelled (rejected/expired).
A resumed run must re-check authorization. Terminal runs cannot execute tools. Durable
append-only events and idempotent step keys will prevent duplicate side effects on retry.
These transitions are a design proposal, not an implemented runtime.

## Trust and failure boundaries
There is no authentication yet: only non-sensitive health routes are exposed. Tenant
APIs are blocked on the identity/RBAC milestone. The browser never receives service
credentials. Production exposure, Kubernetes, vector indexes, schema migrations,
telemetry exporters and model evaluations remain explicitly out of scope.
Postgres init SQL runs only for new volumes; existing databases need an explicit extension
migration. No destructive reset is part of normal startup.

## Skills applied
agent-architecture, fastapi-backend, nextjs-frontend, postgres-data-modeling,
api-openapi, docker-kubernetes, testing-quality, cicd-release.

## References
- https://nextjs.org/docs/app/getting-started/installation
- https://fastapi.tiangolo.com/advanced/events/
