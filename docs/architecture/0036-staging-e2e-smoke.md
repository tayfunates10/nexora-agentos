# ADR 0036: Deployed staging end-to-end smoke gate

Status: accepted for the production-readiness milestone.

Nexora's pull-request CI already verifies unit, integration, browser, Compose, manifest and
monitoring behavior. Those checks still stop at the repository boundary: they do not prove that a
deployed environment has the intended identity configuration, workspace fixture, worker runtime,
provider egress, retrieval configuration, tool governance and metrics exposure wired together.

## Decision

Add a manually triggered `Staging E2E Smoke` workflow backed by
`scripts/staging_smoke.py`. The workflow is attached to the GitHub `staging` environment so
environment protection can control who is allowed to spend provider quota or approve a governed
tool call.

The smoke runner uses only the Python standard library. It does not install an SDK, does not hold a
provider key and never calls the model provider directly. Provider and MCP egress continue to belong
to the deployed worker.

The default staging path proves:

1. API liveness and dependency readiness;
2. bearer-token verification through `GET /api/v1/me`;
3. membership in the configured staging workspace;
4. access to the configured staging agent;
5. visibility of an optional known knowledge source;
6. durable run creation and exact idempotent replay;
7. worker pickup and at least one real model step;
8. terminal requester-scoped result retrieval;
9. contiguous append-only run events through `run.succeeded`;
10. worker metrics increasing during this run for agent execution and model calls;
11. retrieval metrics increasing when retrieval is required.

An optional approval mode additionally waits for the run to enter
`waiting_for_approval`, finds the pending approval for the configured staging tool, approves it
through the public API, requires the worker to resume in a second attempt and verifies that tool-call
metrics increased.

## Staging fixture

The workflow intentionally does not create or mutate operator configuration. The staging
environment must already contain:

- one workspace whose UUID is allowed by the worker's selected model profile;
- one agent in that workspace using the intended staging model profile;
- a retrieval-enabled worker when retrieval verification is requested;
- optionally, one known source key visible to the smoke identity;
- for approval mode, an allowlisted MCP server plus a registered tool whose policy is
  `require_approval` and an agent/prompt combination that deterministically requests that tool.

The approval fixture should be read-only or independently idempotent. A production smoke test must
not depend on an irreversible external side effect.

## GitHub staging environment

Configure these environment variables:

- `NEXORA_STAGING_API_URL`
- `NEXORA_STAGING_WORKER_ADMIN_URL`
- `NEXORA_STAGING_WORKSPACE_ID`
- `NEXORA_STAGING_AGENT_ID`
- `NEXORA_STAGING_PROMPT`
- `NEXORA_STAGING_EXPECT_SOURCE_KEY` when a named retrieval fixture is required
- `NEXORA_STAGING_EXPECT_TEXT` only when the staging model contract intentionally has a stable
  output sentinel
- `NEXORA_STAGING_APPROVAL_TOOL` and `NEXORA_STAGING_APPROVAL_PROMPT` for approval mode

Configure these as environment secrets:

- `NEXORA_STAGING_ACCESS_TOKEN`: a short-lived staging-user access token for the Nexora API
  audience;
- `NEXORA_STAGING_METRICS_TOKEN`: the worker's metrics scrape token.

Do not place a provider API key, MCP bearer token or database credential in this workflow. Those
remain deployment-owned secrets.

## Identity boundary

An unattended GitHub workflow cannot safely automate an organization's interactive browser login,
MFA or SSO challenge. The deployed smoke therefore verifies the API's real bearer-token validation
and subject identity with a short-lived token. The deterministic browser OIDC authorization-code,
PKCE, session-cookie and logout flow remains covered by the Playwright CI fixture.

An operator may additionally perform the real browser login in staging when the identity provider's
interactive policy itself is part of the release acceptance criteria.

## Cost and failure containment

Every run uses a fresh idempotency key. If the smoke fails while its run is still nonterminal, the
runner attempts to cancel the run so a broken gate does not keep consuming provider quota.

The workflow has a ten-minute job deadline and the runner has a bounded polling deadline. It never
prints access tokens, metrics tokens, prompts or model output. The successful summary contains only
run/trace identifiers, provider/model names, selected tool names and event types.

## Observability assertion

The runner snapshots worker counters before creating the run and compares them after success. This
proves that the deployed worker observed new work during the smoke window rather than merely exposing
static metric names. Retrieval and tool counter deltas are required only when those paths are
requested.

Concurrent staging traffic can also increase the counters, so metrics are corroborating deployment
evidence rather than per-run attribution. The run's durable event stream and trace ID remain the
authoritative per-run evidence.

## Boundaries and follow-up

This gate does not deploy to staging, mutate Kubernetes images, or execute rollback. Deployment and
rollback need cluster credentials and environment ownership that remain outside the release
workflow. The protected staging-only rollout/rollback acceptance gate is defined separately in
ADR 0037 and deliberately restores the pre-drill release rather than promoting a candidate.

## Skills applied

agent-architecture, auth-rbac-multitenancy, event-driven-workflows, rag-engineering,
observability-otel, testing-quality, cicd-release, security-threat-modeling.
