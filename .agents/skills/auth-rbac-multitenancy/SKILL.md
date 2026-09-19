---
name: auth-rbac-multitenancy
description: Use when implementing authentication, sessions, OAuth/OIDC, RBAC, workspace membership, tenant isolation, permissions, or service identities.
---

# Auth, RBAC & Multitenancy

Tenant isolation is non-negotiable.

## Identity
Authenticate users and service identities separately. Prefer standards such as OAuth/OIDC.

## Authorization
- Authorize at API/service boundaries and again at privileged tool execution.
- Model roles as coarse grants and permissions/policies as explicit capabilities.
- Default deny.
- Never trust workspace_id supplied by a client without validating membership.

## Data isolation
Every tenant query must be scoped. Add tests specifically attempting cross-tenant access.

## Audit
Record security-sensitive login, membership, role, credential and tool-policy changes.
