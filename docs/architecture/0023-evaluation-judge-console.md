# ADR 0023: Evaluation judge console and latest-run discovery

Status: implemented.

## Context

ADR 0022 adds asynchronous, requester-scoped LLM judge jobs, but its API initially requires a
caller to retain the judge-run ID returned by the queue request. A browser evaluation page is
server-rendered and must remain correct after refresh, navigation or a new browser session. It
therefore needs a requester-safe way to rediscover judge state from the durable evaluation run.

The evaluation console must also keep probabilistic judge evidence visually and semantically
separate from deterministic pass/fail results.

## Decision

Add:

- `GET /api/v1/workspaces/{workspace_id}/eval-runs/{eval_run_id}/judge-runs/latest`
- a CSRF-protected web mutation route that queues judge work through the existing POST endpoint;
- a server-rendered judge section on evaluation-run details.

The latest endpoint returns the newest judge job for the exact workspace, evaluation run and
requester identity. It requires current `eval:manage` permission. If the current requester has no
judge job for that evaluation, it returns 404; another requester's job is indistinguishable from
absence.

## Browser mutation boundary

The browser never receives the API access token. The evaluation page posts the workspace ID,
evaluation-run ID, CSRF token and a fresh UUID idempotency key to a Next.js server route. That route
applies the same origin/referer/CSRF validation used by existing workspace mutations and forwards the
request to the API with the server-side session token.

The generic server API helper now accepts additional request headers, but it always overwrites
`Authorization` and `Content-Type` after reading caller headers. A route therefore cannot replace
the authenticated bearer token while adding `Idempotency-Key`.

A rendered form keeps one UUID idempotency key across duplicate submissions of that page instance.
After a successful queue request the browser is redirected back to the evaluation detail. Refresh
then discovers durable state through the latest endpoint rather than relying on redirect state.

## Eligibility and states

The web contract now includes `source_agent_run_id` for each deterministic case result. The queue
control is shown only when every case came from an agent-run import, matching the judge-v1 backend
requirement.

The console models these states explicitly:

- no eligible judge: explains that manual observation-only evaluations cannot be judged by v1;
- no judge yet: offers **Run quality judge**;
- queued/running: shows durable progress and an explicit refresh link;
- failed: shows the stable failure code without hiding deterministic results and allows a new retry;
- succeeded: shows aggregate quality, baseline quality/delta, judge regressions/improvements, token
  usage and provider latency;
- judge-status API unavailable: preserves deterministic results and reports only the partial outage.

There is no automatic client polling and no hidden model invocation. Starting or retrying a paid
judge is always an explicit button action.

## Presentation boundary

Deterministic results remain first and unchanged. Judge data is placed in a separate card labelled
**AI QUALITY JUDGE · PROBABILISTIC**. Per-case task completion, relevance, clarity, baseline quality,
quality delta and rationale are rendered as plain React text.

The UI converts the stored 0-1000 `quality_milli` scale to a display percentage only. It does not
recompute or reinterpret judge scores. Model/provider/prompt identity is displayed once the worker
pins it.

Judge rationales are never rendered as HTML or Markdown. Failed deterministic raw output keeps the
existing escaped `pre` rendering.

## Verification

- API integration verifies no-job 404, exact-requester latest discovery and completed latest state.
- Web contract tests validate partial judge progress and reject inconsistent succeeded/count states.
- The browser fixture requires a valid `Idempotency-Key` on judge POST, so server header forwarding
  is exercised rather than mocked away.
- Chromium covers queueing from the real form, queued status, durable latest discovery, completed
  aggregate quality and case rationale, plus existing desktop/mobile and access-denial flows.

## Skills applied

llm-evaluation, nextjs-frontend, api-openapi, auth-rbac-multitenancy, security-threat-modeling,
testing-quality, code-review-debugging, cicd-release.
