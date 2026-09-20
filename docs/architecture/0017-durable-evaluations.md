# ADR 0017: Durable deterministic agent evaluations

Status: proposed and implemented in the deterministic evaluation milestone.

## Context

Nexora already persists agent execution state, model steps, tool calls, retrieval provenance and
telemetry. A small in-memory scorer can check expected tools, forbidden tools and citation presence,
but it does not provide a versioned dataset, reproducible historical runs or baseline comparison.
Without those pieces, model, prompt, retrieval and routing changes cannot be evaluated as durable
software regressions.

The evaluation skill requires probabilistic quality to be measured with versioned golden datasets,
deterministic checks separated from judge-model scores, failed-case outputs retained for debugging,
and candidate results compared with a baseline rather than relying only on absolute scores.

## Decision

The first public evaluation surface is deliberately deterministic and provider-free. Owners and
admins create immutable versioned evaluation suites containing bounded cases. Each case records
input text plus expected tools, forbidden tools and expected citation identifiers.

A completed evaluation run submits one observation for every case in the suite. The API evaluates
those observations with the provider-independent scorer and persists the complete result atomically.
This milestone does not call a model provider and therefore does not add provider credentials or
network egress to the API process.

Suites, cases, runs and case results are append-only in PostgreSQL. A changed golden dataset is a new
suite version rather than an in-place edit.

## API contract

The public endpoints are:

- `POST /api/v1/workspaces/{workspace_id}/eval-suites`
- `GET /api/v1/workspaces/{workspace_id}/eval-suites`
- `GET /api/v1/workspaces/{workspace_id}/eval-suites/{suite_id}`
- `POST /api/v1/workspaces/{workspace_id}/eval-suites/{suite_id}/runs`
- `GET /api/v1/workspaces/{workspace_id}/eval-runs/{eval_run_id}`

Suite and run creation require `Idempotency-Key`. Repeating the same normalized request returns the
existing resource; reusing the key for different content returns HTTP 409.

Current workspace members may read suite definitions. Creating suites, executing evaluations and
reading run details require `eval:manage`, granted to owners and admins. Run details are restricted
because failed-case raw output may contain tenant data.

## Complete-case comparability

Every evaluation run must contain exactly one observation for every case in the selected suite.
Partial runs are rejected. This keeps aggregate pass/fail rates comparable and prevents a candidate
from silently omitting difficult cases.

Tool and citation collections are treated as sets for deterministic scoring. Case order remains
stable through an explicit `case_no`, while stored observation lists are canonicalized.

## Baseline semantics

A baseline, when supplied, must be a completed run of the same workspace and the exact same suite
version. Per case:

- baseline pass -> candidate fail is a regression;
- baseline fail -> candidate pass is an improvement;
- otherwise neither flag is set.

Aggregate regression and improvement counts are stored with the run. Baseline comparison never
mixes suite versions because doing so would compare different test contracts.

## Failed-case evidence

Raw model/application output is optional input to the deterministic evaluator. It is persisted only
for failed cases; passing-case raw output is discarded. This limits unnecessary retention while
preserving the evidence needed to debug regressions. Output is bounded to 50,000 characters.

The scorer itself receives only selected tool names and citation identifiers. Raw output is not used
as an authorization signal and does not alter deterministic pass/fail rules.

## Security and trust boundaries

Every query is workspace-scoped and passes through the existing membership/RBAC boundary. Unrelated
tenants receive the platform's existing anti-enumeration behavior. Security controls remain outside
the LLM.

Evaluation submission is an assessment path, not an execution path: it cannot call tools, send
external messages, modify an agent run or invoke model providers. Failed raw output remains tenant
data and is not copied to telemetry.

## Future judge and automated execution layers

Model-judge scoring is intentionally not part of this increment. A later judge layer must pin judge
prompt/version/model, keep judge scores separate from deterministic assertions, record cost and
latency, and run outside the API process through an operator-controlled worker.

A later automation layer may harvest observations directly from completed agent runs. That layer
must preserve the same immutable suite version, baseline semantics, tenant boundary and complete-case
comparison rules.

## Skills applied

llm-evaluation, agent-architecture, api-openapi, fastapi-backend, postgres-data-modeling,
auth-rbac-multitenancy, security-threat-modeling, testing-quality, cost-performance.
