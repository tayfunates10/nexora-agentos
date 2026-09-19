# Nexora AgentOS — Claude Code Project Rules

Nexora AgentOS is an enterprise AI-agent platform. Treat every change as production software, not a demo.

## Core principles
- Prefer explicit architecture and typed contracts over hidden coupling.
- Multi-tenancy, RBAC, auditability, observability, and safe tool execution are first-class requirements.
- Agents must never bypass approval, authorization, tenant isolation, or policy checks.
- Every external action must be traceable to user, agent, tool, workspace, request, and approval state.
- Keep LLM/provider code behind adapters. Do not bind domain logic to a single model vendor.
- Use tests for business rules and evals for probabilistic AI behavior.
- Prefer boring, maintainable infrastructure unless complexity is justified by a documented requirement.

## Target stack
- Web: Next.js + TypeScript
- API: Python + FastAPI
- Data: PostgreSQL + pgvector
- Cache/queues: Redis
- Agent/tool protocol: MCP
- Observability: OpenTelemetry + Prometheus/Grafana
- Delivery: Docker, GitHub Actions, Kubernetes

## Workflow
1. Read relevant skills from .claude/skills before implementation.
2. State assumptions in code/docs when requirements are ambiguous.
3. Add or update tests with every behavior change.
4. Never commit secrets.
5. Prefer small, reviewable changes with clear commit messages.
