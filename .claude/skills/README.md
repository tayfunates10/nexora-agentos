# Nexora AgentOS Skill Pack

Project-local Claude Code skills for building Nexora AgentOS.

Claude Code discovers skills from `.claude/skills/<skill-name>/SKILL.md`.
Each skill is intentionally scoped so only relevant instructions are loaded for a task.

## Domains
- Agent platform: agent-architecture, mcp-server-development, tool-contracts, human-approval
- AI: rag-engineering, llm-evaluation, model-routing, prompt-guardrails
- Backend/data: fastapi-backend, api-openapi, postgres-data-modeling, redis-jobs
- Product security: auth-rbac-multitenancy, security-threat-modeling
- Frontend: nextjs-frontend
- Reliability: observability-otel, testing-quality, cost-performance
- Platform: event-driven-workflows, docker-kubernetes, cicd-release
- Engineering hygiene: code-review-debugging

These are Nexora-specific project skills and should evolve with architecture decisions.
