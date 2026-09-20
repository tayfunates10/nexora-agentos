# ADR 0020: Requester-scoped agent-run evaluation imports

Status: implemented.

## Context

Nexora has two durable but deliberately separate capabilities: terminal agent-run results and
deterministic evaluation suites. Manual evaluation submissions are useful for imported fixtures, but
they do not prove that an observation came from a real agent execution. The result API introduced a
safe requester-owned terminal snapshot, which makes a constrained bridge possible.

The bridge must not turn evaluation-management permission into authority to read another user's
requester-scoped output. It also must not infer retrieval provenance from model-generated citation
text. The current executor supplies retrieved evidence to the model but does not persist the exact
retrieved chunk set as immutable run provenance.

## Decision

POST /api/v1/workspaces/{workspace_id}/eval-suites/{suite_id}/run-imports creates a deterministic
evaluation run from completed agent runs.

The request contains a candidate label, optional baseline evaluation run and exactly one
case_key/agent_run_id mapping for every case in the selected immutable suite version. Agent-run IDs
may not be reused across cases.

For every mapped run the API requires:

- current eval:manage permission;
- the same workspace;
- the exact current principal issuer and subject as the original run requester;
- terminal succeeded state with a complete persisted model journal;
- exact equality between the suite case input and the persisted agent-run input.

Owner or admin status does not bypass requester ownership. Missing, foreign-workspace and
other-requester source runs return 404.

The importer derives selected tool names and final output only from the persisted model journal by
using the same terminal-result summarizer as the public result endpoint. It never accepts
client-supplied selected tools or raw output for this path. The source agent run for each case is
stored in the append-only eval_agent_run_sources table and returned as source_agent_run_id on
detailed evaluation results.

Existing deterministic scoring, same-suite baseline semantics, complete-case comparison and
failed-output-only retention remain unchanged. Import requests are idempotent, and their fingerprint
includes every case-to-run mapping.

## Citation boundary

Automated import currently refuses any suite containing expected_citations with
verified_retrieval_provenance_required.

This is intentional. Citation-looking text in a final model answer is not evidence that a source was
actually retrieved. Nexora's current RAG layer retains source provenance during retrieval, but the
executor does not yet persist that exact evidence set against the durable agent run. A future
retrieval-provenance milestone may enable citation evaluation only after that evidence can be tied
immutably to the run without weakening source ACL or retention semantics.

## Limits and failure behavior

Failed, cancelled, queued, running and approval-waiting source runs cannot be imported. Successful
runs whose terminal journal is incomplete fail closed. Agent output larger than the evaluation
evidence limit and tool sets larger than the deterministic evaluation contract are rejected rather
than truncated.

The importer invokes no model provider, retrieval provider or tool. It adds no external egress and
does not mutate source agent runs.

## Verification

Integration coverage exercises successful and failed deterministic cases, stored source-run
provenance, requester isolation, RBAC denial, exact input matching, terminal-state enforcement,
idempotent replay/conflict and citation fail-closed behavior. The provenance mapping is protected by
the same append-only evaluation mutation trigger as the existing evaluation records.

Skills: llm-evaluation, agent-architecture, api-openapi, fastapi-backend,
postgres-data-modeling, auth-rbac-multitenancy, security-threat-modeling, testing-quality,
code-review-debugging, cicd-release.
