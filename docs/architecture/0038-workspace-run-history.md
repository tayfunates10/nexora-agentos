# ADR 0038: Workspace run history

Status: implemented.

## Context

Runs could be created, read by identifier and cancelled, but nothing listed them. Any operator
view of "what has this workspace been doing" required a run identifier that only the caller who
started the run had. That is workable for a scripted integration and unusable for a console: an
owner cannot see a queue backing up, a member cannot find the run they started ten minutes ago,
and a failed run leaves no trace anyone can navigate to.

The result endpoint is deliberately requester-scoped (ADR 0019) because retrieval evidence can be
ACL-bound to one requester. A history list must not become a way around that boundary.

## Decision

GET /api/v1/workspaces/{workspace_id}/runs returns workspace run metadata, newest first, to any
current workspace member. It carries exactly what the existing single-run resource already
exposes to members — identifiers, status, attempt count, cancellation, failure code and
timestamps — plus the agent's name, which is workspace configuration rather than run content,
and a `requested_by_me` boolean derived from the verified issuer/subject of the caller.

The prompt, the answer, retrieval evidence and the requester's identity string are not in this
response. `requested_by_me` lets a caller find their own runs without publishing who started
anyone else's; `requested_by_me=true` filters the list to them. Additional filters are `status`
(validated against the run state enum, so an unknown value is a 422, not an empty page) and
`agent_id`.

Pagination is keyset over `(created_at, id)` descending, matching evaluation history (ADR 0018).
The cursor is the last returned run identifier; it is resolved inside the authorized workspace
scope, so a cursor from another workspace or an unknown one returns 404 rather than leaking
whether that run exists. `limit` is 1–100 and defaults to 25. The existing
`agent_runs(workspace_id, created_at, id)` index serves the scan backwards; no migration is
needed.

Authorization is `workspace:read`, checked in the same transaction as the query, so a revoked
membership stops returning history immediately. A non-member receives 404 for the workspace, the
same answer the rest of the workspace API gives.

## State and trust boundaries

The endpoint is read-only: it starts no run, touches no provider, and changes no run state. It
does not widen the result boundary — a member who can see that another member's run failed still
cannot read its output, and `GET .../runs/{id}/result` continues to require the original
requester. Terminal runs stay listed with their recorded failure code; history is not deleted
when retrieved context is.

## Verification

Integration tests against PostgreSQL cover newest-first ordering, keyset paging across pages, an
unknown cursor, cursor scoping to the workspace, the status and agent filters, rejection of an
invalid status and an out-of-range limit, `requested_by_me` for both the requester and another
member, absence of prompt and requester fields in the payload, a terminal failed run, an empty
workspace and cross-tenant refusal for a non-member.

Skills: agent-architecture, api-openapi, fastapi-backend, auth-rbac-multitenancy,
postgres-data-modeling, testing-quality.
