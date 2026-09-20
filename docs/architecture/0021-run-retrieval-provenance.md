# ADR 0021: Immutable run retrieval provenance with ephemeral context

Status: implemented.

## Context

Nexora can retrieve permission-filtered RAG chunks and can import completed agent runs into
deterministic evaluation suites. Until this increment, the executor passed retrieved evidence to the
model without durably tying the exact source/version/chunk set to the run. That made automated
citation evaluation unsafe: model-authored citation-looking text cannot prove which evidence was
actually retrieved.

Retries and approval resumes add another correctness boundary. Re-running vector search can return a
different chunk set after re-indexing, ACL changes or source-version changes, while persisted model
steps still reflect the earlier evidence.

## Decision

A retrieval-enabled run records one immutable retrieval provenance snapshot before the first model
generation that uses it.

The durable snapshot stores:

- run and workspace identity;
- SHA-256 of the run query and exact rendered retrieval context;
- embedding input token count and retrieved chunk count;
- ordered chunk ID, source ID, source key, source version, chunk index and content hash.

Similarity scores, source metadata and ACL rows are not copied into the durable snapshot. The
provenance tables are append-only.

The exact rendered retrieval text is stored separately in `agent_run_retrieval_context` only while
the run may need deterministic retry or approval resume. A database trigger deletes that raw context
when the run enters `succeeded`, `failed` or `cancelled`. Durable provenance hashes and identifiers
remain available for audit and evaluation without retaining an additional long-lived copy of the
retrieved document text.

## Replay and authorization

On a retry or approval resume the executor loads the stored context instead of issuing another
embedding/search request. Before reuse it re-runs the normal execution fence, including current
workspace membership, and verifies that every snapshotted chunk still exists as the current source
version with the same source ID, source key, version, chunk index and content hash. Restricted chunks
must still be visible to the original requester through the current source ACL.

If any source was deleted, replaced, re-indexed to different chunks, or is no longer authorized, the
run fails closed with `retrieval_snapshot_unavailable`. Nexora never silently substitutes a different
retrieval result underneath already persisted model steps.

A first-time snapshot is also revalidated against the current source/chunk rows inside the snapshot
write transaction before the model receives the stored context.

## Evaluation citations

Automated agent-run imports derive citation identifiers only from durable retrieval provenance. The
canonical source-version identifier is:

`rag:<percent-encoded-source-key>@<percent-encoded-source-version>`

For example, source `handbook` version `v1` becomes `rag:handbook@v1`.

A case that requires citations but maps to an older run without a retrieval snapshot is rejected with
`verified_retrieval_provenance_required`. A run with a verified zero-hit snapshot is valid
provenance; it simply has no citation identifiers and therefore fails any required-citation
assertions normally.

This proves that the source/version was supplied as retrieved evidence to the run. It does not claim
that the final model prose faithfully quoted or semantically used that evidence; groundedness and
judge-model scoring remain separate evaluation layers.

## Privacy and retention

Raw retrieved text is never added to telemetry, run events or evaluation results. The temporary
context row exists only to make non-terminal retries deterministic and is removed transactionally by
the terminal run-state transition. Historical evaluation records keep only bounded citation
identifiers plus failed-case model output under the existing requester-scoped detail boundary.

Deleting or re-indexing a knowledge source invalidates future snapshot replay because current
source/chunk validation fails. Historical run provenance identifiers and hashes remain append-only so
past evaluations do not become mutable when the live knowledge index changes.

## Verification

Coverage includes deterministic snapshot replay without a second embedding/search request, canonical
citation-ID escaping, requester-scoped citation imports, fail-closed legacy runs, append-only
provenance mutation rejection and terminal cleanup of raw retrieval context. Full platform CI remains
the merge gate.

## Skills applied

rag-engineering, llm-evaluation, agent-architecture, postgres-data-modeling,
auth-rbac-multitenancy, security-threat-modeling, testing-quality, code-review-debugging,
cicd-release.
