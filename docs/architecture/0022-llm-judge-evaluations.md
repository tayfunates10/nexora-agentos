# ADR 0022: Worker-scoped pinned LLM judge evaluations

Status: implemented.

## Context

Nexora's deterministic evaluation layer intentionally checks only facts that software can assert:
required tools, forbidden tools, verified citation identifiers and baseline pass/fail changes. Those
checks are durable and comparable, but they do not measure answer quality such as task completion,
relevance or clarity.

The evaluation skill requires judge-model scores to remain separate from deterministic assertions,
pin the judge prompt and model for comparable results, preserve baseline comparison, and keep paid
provider calls outside the API process.

## Decision

Nexora adds an asynchronous evaluation-judge job for completed evaluation runs imported from real
agent runs.

The API queues work only. It never invokes a judge provider. The existing operator worker claims
judge jobs from PostgreSQL with bounded leases and retries. One worker iteration evaluates at most
one suite case, so a large evaluation cannot monopolize the agent worker loop.

The first judge contract is pinned as `nexora-eval-judge-v1`. It scores three independent
dimensions from 0 through 4:

- task completion;
- answer relevance;
- clarity.

The model does not supply an overall score. PostgreSQL deterministically derives a 0-1000
`quality_milli` value from the three bounded dimensions. This prevents a judge from changing the
aggregation rule.

## Source-output boundary

Judge v1 accepts only evaluation runs created through the agent-run import path. Every suite case
must have an immutable `eval_agent_run_sources` mapping.

The worker reconstructs the final answer from the persisted model journal using the same terminal
result summarizer used by the requester result API and the deterministic importer. It does not rely
on `eval_case_results.raw_output`, because passing deterministic cases deliberately discard raw
output.

Manual observation-only evaluation runs are rejected for judge scheduling until a future bounded
judge-input snapshot exists.

## Baseline comparison

When the deterministic evaluation run references a baseline, the baseline must also be a complete
agent-run import owned by the same requester.

For each case, the worker scores candidate and baseline with the exact same provider, model and
prompt version in the same judge job. The database stores the candidate scores plus baseline scores
and derives the quality delta. Candidate quality below baseline is reported as a judge regression;
quality above baseline is an improvement.

This comparison is separate from deterministic regression flags. A run can pass all deterministic
assertions and still score lower on judged quality, or vice versa.

## Model and prompt pinning

Operators opt into the judge through `evaluation_judge` in the worker runtime configuration.
The configuration selects:

- provider and exact model;
- explicit workspace allowlist;
- prompt version;
- provider timeout;
- maximum output tokens;
- maximum input characters.

The selected model must already be a declared model candidate with both `text` and
`structured_output` capabilities.

Provider, model and prompt version are copied into the durable judge job on first claim and become
immutable. A job pinned under one judge configuration is never silently resumed under another.

## Security and privacy

Scheduling and reading judge results require current `eval:manage` permission and the exact
requester identity that created the source evaluation run. Owner/admin role does not grant access to
another requester's judge details.

The worker rechecks `eval:manage` immediately before provider work. If the requester is demoted or
removed after enqueue, the job fails with `judge_permission_revoked` without provider egress.

The judge receives only the suite input and final answer. It receives no credentials, tool
arguments, tool results, retrieved raw context, or authorization data. No tools are exposed to the
judge. Candidate text is explicitly treated as untrusted content in the pinned prompt.

Judge rationales are bounded to 1000 characters and remain requester-scoped tenant data. Prompts and
answers are not copied into telemetry.

## Durability and retries

Judge jobs use PostgreSQL status, lease owner, lease expiry, retry count and deterministic jitter.
Case score rows are append-only.

A successful case is committed before the job is requeued for the next case. Crash recovery skips
already committed cases, so completed scores are not recomputed. A provider call can still be
duplicated if a process dies after provider success but before the score transaction commits; this
is an unavoidable at-least-once boundary without provider-side idempotency.

Retryable provider errors receive bounded retries. Invalid structured judge output and source-journal
integrity failures fail closed.

## Cost and latency evidence

Each case score stores normalized provider input/output token counts and measured provider latency.
These are kept separate from deterministic evaluation results and can be converted to monetary cost
using operator pricing without changing historical judge scores.

## Limits

Judge v1 does not claim groundedness or citation faithfulness. Raw retrieval context is intentionally
purged when an agent run becomes terminal, so groundedness requires a separate evidence-reconstruction
contract rather than pretending citation identifiers alone prove semantic support.

Judge v1 also does not gate pull requests automatically. Fast deterministic evals remain the
appropriate CI gate; judge suites are asynchronous quality evidence.

## Verification

Coverage includes runtime configuration validation, idempotent API scheduling, requester isolation,
permission revocation before egress, candidate/baseline scoring under one pinned judge, partial
multi-case progress, append-only scores and deterministic quality deltas.

## Skills applied

llm-evaluation, agent-architecture, model-routing, postgres-data-modeling,
auth-rbac-multitenancy, security-threat-modeling, testing-quality, cost-performance,
code-review-debugging, cicd-release.
