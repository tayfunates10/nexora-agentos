# ADR 0033: Hybrid lexical and vector retrieval

Status: implemented.

## Context

Vector retrieval is strong at semantic similarity but can miss identifiers, error codes, product
names and other exact tokens that matter operationally. Pure lexical retrieval has the opposite
failure mode. Nexora therefore needs a deterministic fusion layer rather than choosing either
signal globally.

## Decision

Worker retrieval supports two operator-selected strategies: `vector` and `hybrid`. The shipped
runtime default is `hybrid`.

Hybrid retrieval creates two independently ranked candidate lists inside PostgreSQL:

1. cosine-distance vector candidates for the configured embedding model and dimensions;
2. PostgreSQL full-text candidates using the language-neutral `simple` dictionary.

Both candidate queries apply workspace, current-version and source-ACL filters before a chunk can
enter either candidate list. Results are fused with Reciprocal Rank Fusion using a fixed `k=60`.
The ranking contract is deterministic: ties break by source id and chunk index. A lexical query is
bounded to 4096 characters and each signal contributes at most four times the requested result
limit, capped at 200 candidates.

The public result remains the same `RetrievedChunk` contract. Its score is cosine similarity in
vector mode and the fused RRF score in hybrid mode; consumers must treat score as an ordering signal,
not as a probability.

## Index

Migration 015 adds a GIN expression index on `to_tsvector('simple', content)`. No new tenant table
or privilege is introduced.

The current migration runner executes each migration transactionally, so this index is built with
ordinary `CREATE INDEX`, not `CREATE INDEX CONCURRENTLY`. That can briefly block writes to a large
`rag_chunks` table. Operators with large existing indexes should schedule the migration in a
maintenance window. Supporting explicitly non-transactional online-index migrations is a separate
migration-engine improvement and should not be hidden inside retrieval code.

## Security

Hybrid retrieval does not widen the authorization boundary. ACL filtering happens independently in
both vector and lexical candidate queries, before fusion. A lexical exact match in a restricted
source therefore cannot bypass source ACLs. Query text is used only as a bound PostgreSQL parameter
through `plainto_tsquery`; no raw tsquery syntax is accepted.

## Quality verification

Integration coverage includes a deliberately adversarial retrieval case where the query embedding
points to a semantically wrong chunk while an exact incident identifier exists in another chunk.
Vector-only retrieval keeps the semantic chunk first; hybrid RRF moves the exact identifier to the
top. The same test includes a restricted exact-match source and verifies an unauthorized member
cannot retrieve it.

## Skills applied

rag-engineering, postgres-data-modeling, security-threat-modeling, cost-performance,
testing-quality, code-review-debugging.
