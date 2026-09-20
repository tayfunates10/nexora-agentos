# ADR 0029: Distributed authenticated API rate limiting

Status: implemented for authenticated API traffic.

## Problem

Nexora validates bearer identities and tenant authorization, but a valid identity could still send an
unbounded request rate to every API replica. A per-process counter would split limits across replicas,
and trusting client-supplied forwarding headers would let requesters influence the limiter identity.

## Decision

Apply one shared fixed-window limit after RS256 bearer verification and before workspace/database
authorization. The key is derived from SHA-256 of the verified issuer and subject; the raw subject,
token and workspace identifier are never stored in Redis. Every API replica uses the same Redis
counter and an atomic Lua script performs increment plus expiry maintenance.

The application setting is `NEXORA_API_RATE_LIMIT_REQUESTS` with
`NEXORA_API_RATE_LIMIT_WINDOW_SECONDS`. A request limit of zero disables the limiter for isolated
library/test use. Compose and the Kubernetes base enable 120 requests per verified identity per
60 seconds. Operators may tune these values for their workload.

When a verified identity exceeds the limit the API returns 429 with `Retry-After`. When the limiter
is enabled but Redis cannot make a decision, authenticated requests fail closed with 503 instead of
silently bypassing the control. Existing HTTP metrics therefore expose both rejection and dependency
failure status without adding tenant identifiers to metric labels.

## Boundaries

The limiter intentionally does not trust `X-Forwarded-For` or similar client-controlled headers.
It protects authenticated application traffic, not volumetric attacks, invalid-token signature
verification floods, health probes or the separately authenticated metrics endpoint. A production
Ingress/Gateway must still enforce connection/body limits and unauthenticated edge rate limits.

This control is identity-wide rather than workspace-specific. One verified subject therefore cannot
multiply its allowance by joining more workspaces, and cross-tenant membership never changes the key.

## Validation

Unit tests cover disabled behavior, identity hashing, counter/TTL decisions and fail-closed Redis
errors. An integration test uses two independent Redis clients to prove that replicas share a single
counter. Authentication integration verifies 429 and `Retry-After` at the HTTP boundary.

Skills: auth-rbac-multitenancy, fastapi-backend, security-threat-modeling, testing-quality,
redis-jobs, observability-otel.
