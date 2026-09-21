# ADR 0036: Deployed staging end-to-end smoke gate

Status: accepted for the production-readiness milestone.

Nexora's pull-request CI verifies unit, integration, browser, Compose, manifest and monitoring
behavior. Those checks stop at the repository boundary: they do not prove that a deployed
environment has identity, workspace authorization, worker runtime, provider egress, retrieval and
tool governance wired together.

## Decision

A manually triggered `Staging E2E Smoke` workflow runs `scripts/staging_smoke.py` against a
pre-provisioned staging fixture. The workflow is attached to the GitHub `staging` environment so
environment protection can control who is allowed to spend provider quota or approve a governed
tool call.

The runner uses only the Python standard library. It holds no provider key, MCP credential,
database credential or Kubernetes credential and never calls a model provider directly. Provider
and MCP egress remain owned by the deployed worker.

The default hosted-runner path proves:

1. API liveness and dependency readiness;
2. real bearer-token verification through `GET /api/v1/me`;
3. membership in the configured staging workspace;
4. access to the configured staging agent;
5. visibility of the configured retrieval fixture source;
6. durable run creation and exact idempotent replay;
7. worker pickup and at least one persisted real model step;
8. terminal requester-scoped result retrieval;
9. contiguous append-only run events through `run.succeeded`;
10. when retrieval is requested, the answer contains the fixture's unique non-secret sentinel.

An optional approval mode additionally waits for `waiting_for_approval`, finds the pending approval
for the configured staging tool, approves it through the public API, requires the worker to resume
in a second attempt and confirms that the selected tool is present in the immutable model journal
summary.

## Retrieval proof without exposing worker administration

The Kubernetes worker admin/metrics service is intentionally cluster-internal. A GitHub-hosted
runner must not require that service to be exposed to the Internet merely to prove retrieval.

When `require_retrieval=true`, the staging environment therefore supplies both
`NEXORA_STAGING_EXPECT_SOURCE_KEY` and `NEXORA_STAGING_EXPECT_TEXT`. The source must contain a
unique, non-secret sentinel that the staging prompt asks the model to return. The smoke gate first
proves the source is visible through the public knowledge API and then proves the successful run
returned the sentinel. This exercises deployed retrieval through the normal agent path without
creating a new operational endpoint.

For a private or self-hosted runner that already has legitimate cluster-network access,
`verify_worker_metrics=true` may additionally verify worker readiness and counter deltas for agent
runs, model calls, retrieval and optional tool calls. Metrics are corroborating evidence, not the
primary hosted-runner proof.

## Staging fixture

The workflow intentionally does not create or mutate operator configuration. The staging
environment must already contain:

- one workspace whose UUID is allowed by the worker's selected model profile;
- one agent in that workspace using the intended staging model profile;
- a retrieval-enabled worker when retrieval verification is requested;
- one known source key containing a unique sentinel when retrieval is requested;
- for approval mode, an allowlisted MCP server plus a registered tool whose policy is
  `require_approval` and a prompt that deterministically requests that tool.

The approval fixture should be read-only or independently idempotent. A release smoke test must not
depend on an irreversible external side effect.

## GitHub staging environment

Configure these environment variables:

- `NEXORA_STAGING_API_URL`
- `NEXORA_STAGING_WORKSPACE_ID`
- `NEXORA_STAGING_AGENT_ID`
- `NEXORA_STAGING_PROMPT`
- `NEXORA_STAGING_EXPECT_SOURCE_KEY` and `NEXORA_STAGING_EXPECT_TEXT` for retrieval mode
- `NEXORA_STAGING_APPROVAL_TOOL` and `NEXORA_STAGING_APPROVAL_PROMPT` for approval mode
- `NEXORA_STAGING_WORKER_ADMIN_URL` only for a runner with authorized private network access

Configure `NEXORA_STAGING_ACCESS_TOKEN` as an environment secret. It should be a short-lived
staging-user access token for the Nexora API audience. Configure
`NEXORA_STAGING_METRICS_TOKEN` only when private worker-metric verification is enabled.

Do not place a provider API key, MCP bearer token, database credential or cluster credential in the
smoke workflow. Those remain deployment-owned secrets.

## Identity boundary

An unattended workflow cannot safely automate an organization's interactive browser MFA or SSO
challenge. The deployed smoke verifies the API's real bearer-token validation and subject identity
with a short-lived token. The authorization-code/PKCE/session-cookie/logout browser path remains
covered by deterministic Playwright CI. An operator may additionally perform real interactive login
in staging when the identity provider's own policy is part of release acceptance.

## Cost and failure containment

Every smoke run uses a fresh idempotency key. If the gate fails while its run is still nonterminal,
the runner attempts to cancel it so a broken deployment does not keep consuming provider quota.

The workflow and polling loops have bounded deadlines. It never prints access tokens, metrics
tokens, prompts or model output. Its success summary contains only run/trace identifiers,
provider/model names, selected tool names and event types.

## Boundaries

This gate proves application behavior after deployment. It does not hold cluster credentials,
perform Kubernetes rollout or mutate image references. The isolated staging overlay and the
operator-owned rollout/rollback drill are defined in ADR 0037.

## Skills applied

agent-architecture, auth-rbac-multitenancy, event-driven-workflows, rag-engineering,
observability-otel, testing-quality, cicd-release, security-threat-modeling.
