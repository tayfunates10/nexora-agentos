# ADR 0018: Evaluation history and read-only console

Status: implemented.

## Context

The deterministic evaluation API stores suites and result details, but users cannot discover
historical run IDs or inspect results from the browser. Baseline comparisons need a browsable
history tied to an exact suite version.

## Decision

Add GET /api/v1/workspaces/{workspace_id}/eval-suites/{suite_id}/runs. It returns bounded
summary pages ordered by (created_at DESC, id DESC). The optional UUID cursor resolves to an
immutable run within the same workspace and suite. Unknown or foreign cursors return 404.
Newer insertions do not shift subsequent pages. The existing suite/time index supports this query.
The response omits case details and raw evidence; there is no new migration.

History requires eval:manage, exactly like run details. Authorization is checked before
suite/cursor lookup. Members may browse suite definitions, but cannot fetch run summaries.
Every query remains workspace-scoped. Existing run-detail responses remain compatible.

The server-rendered console offers suite discovery, expandable case definitions, paginated
history, aggregate results, baseline links, per-case failures and retained failed evidence.
Tokens remain server-side and requests use no-store. Zod validates API responses.
Raw evidence is rendered as escaped text in a wrapping, bounded-height pre element; it is
never interpreted as HTML or executable Markdown.

Loading, empty, invalid-link, unavailable and forbidden states are explicit. When history
fails, accessible suite definitions remain visible. Counts without a baseline display a dash,
so zero regressions is not confused with no comparison.

## Boundaries

This is an inspection surface. Suite creation and observation submission continue through the
existing API. It does not launch models, create judge scores, execute tools or change permissions.
All displayed results remain deterministic evaluations of submitted observations.

## Verification

- PostgreSQL integration: newest-first pages, insertion between pages, no raw evidence in history,
  invalid limits/cursors, cross-suite/cross-tenant rejection, admin access and immediate role revocation.
- Web contracts: malformed counts/cursors/timestamps, complete details and exact evidence retention.
- Chromium: empty states, suite/history navigation, older pages, baseline comparison, escaped
  script-like evidence, mobile/desktop layout, partial outage and member denial.

Skills: llm-evaluation, nextjs-frontend, api-openapi, fastapi-backend,
auth-rbac-multitenancy, testing-quality, code-review-debugging, cicd-release.
