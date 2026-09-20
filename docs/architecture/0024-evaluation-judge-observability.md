# ADR 0024: Evaluation judge operational metrics

Status: implemented.

## Context

The pinned LLM judge stores durable per-case token usage and provider latency, but operators cannot
observe live judge traffic through the existing Prometheus surface. A production worker needs to
explain whether judge failures come from provider calls, invalid structured output, permission
revocation or crash recovery without exposing tenant prompts or creating unbounded metric labels.

The general model metrics also previously omitted judge provider calls even though they are real
model-generation traffic.

## Decision

Evaluation judge provider calls now contribute to the existing model telemetry:

- `nexora_model_calls_total{provider,outcome}`;
- `nexora_model_call_duration_seconds{provider}`;
- `nexora_model_tokens_total{provider,kind}`.

Judge-specific bounded series are added:

- `nexora_evaluation_judge_calls_total{provider,target,outcome}`;
- `nexora_evaluation_judge_call_duration_seconds{provider,target}`;
- `nexora_evaluation_judge_tokens_total{provider,target,kind}`;
- `nexora_evaluation_judge_jobs_total{outcome}`.

`target` is restricted to `candidate` or `baseline`. Judge-call outcomes are restricted to
`success`, `provider_error`, `timeout` and `invalid_response`. Terminal job outcomes are only
`succeeded` or `failed`. Unexpected values collapse to `other`.

Workspace, requester, evaluation-run, case and judge-run identifiers are deliberately absent from
labels.

## Semantics

A provider response that arrives successfully contributes a successful general model call and its
reported tokens, even if its structured judge payload later fails validation. The judge-specific
series records that second-stage failure as `invalid_response`. This keeps provider reliability
separate from judge-contract reliability.

Provider exceptions contribute an error or timeout to both the general model series and judge-call
series and do not invent token counts.

A judge job terminal metric is emitted only when the worker commits a transition to `succeeded` or
`failed`. Intermediate case completion and bounded retry requeueing are not counted as terminal
jobs.

Crash recovery is included: if a worker discovers an expired judge lease whose retry budget is
exhausted, the database transition to terminal `failed` also increments the failed-job metric.

## Privacy and cardinality

No prompt, final answer, rationale, citation, tool information or tenant identifier enters
Prometheus. Provider labels remain operator-controlled and pass through the existing bounded label
normalizer. Target/outcome labels use fixed enumerations.

Quality scores remain durable evaluation data rather than Prometheus labels or aggregate metric
series. Cross-tenant quality aggregation would be ambiguous and is intentionally not introduced by
this increment.

## Cost boundary

Token counters provide the measurable input needed for cost accounting. Monetary conversion remains
operator pricing data because provider/model prices can change independently of historical judge
scores. Durable judge case rows continue to preserve normalized input/output tokens for exact
per-evaluation accounting.

## Verification

- metric unit tests cover judge calls, tokens, terminal jobs and unknown-label collapse;
- PostgreSQL judge integration verifies candidate/baseline calls contribute to both general model
  telemetry and judge-specific telemetry;
- permission revocation verifies terminal failure without any provider-call metric;
- expired-lease recovery verifies a final crash-recovery failure is counted without provider egress.

## Skills applied

llm-evaluation, observability-otel, cost-performance, testing-quality, security-threat-modeling,
code-review-debugging, cicd-release.
