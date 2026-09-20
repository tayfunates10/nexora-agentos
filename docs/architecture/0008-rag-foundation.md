# ADR 0008: Permission-aware RAG storage and retrieval foundation

Status: accepted for the first RAG increment.

## Context

Milestone 5 requires retrieval-augmented generation without weakening workspace isolation. Retrieved
documents are untrusted input: they can contain prompt injection, stale data, or content the current
actor is not allowed to see. Permission filtering therefore belongs in the retrieval query, before
any context reaches a model.

## Decision

Nexora introduces tenant-scoped RAG sources, source ACLs and chunks in PostgreSQL/pgvector.

Every chunk carries:

- `workspace_id`;
- `source_id` and explicit `source_version`;
- copied access scope;
- deterministic chunk index and normalized text offsets;
- SHA-256 content hash;
- embedding model and dimensions;
- flexible metadata for citation details.

Sources are versioned by `source_key + version`. Exactly one version can be current for a
workspace/source key. Re-indexing the same version preserves the source ID and transactionally
replaces ACL/chunk rows. Indexing a newer version marks older versions non-current.

## Authorization and ACL

Knowledge-index mutation requires `knowledge:manage`, granted to owners and admins. Retrieval
requires current workspace membership.

A source is either:

- `workspace`: visible to every current workspace member; or
- `restricted`: visible only when the current issuer/subject has an explicit source ACL row.

The SQL retrieval query applies workspace membership, current-version, embedding-model/dimension
and ACL filters before ordering by vector distance. Cross-workspace identifiers cannot broaden a
query.

## Retrieval

The first increment uses exact cosine distance with pgvector's `<=>` operator and returns cosine
similarity as `1 - distance`. Query and stored vectors must use the same configured embedding
model and dimensions.

The vector column intentionally has no fixed typmod so multiple operator-configured embedding
models can coexist while dimensions are checked explicitly on every chunk. No HNSW or IVFFlat
index is added yet. RAG quality must first be measured with retrieval test sets; ANN configuration
will be selected from measured recall and latency rather than intuition.

## Chunking and provenance

Chunking is deterministic and offset-preserving over normalized text. Bounds and overlap are
explicit. Repeating the same input and chunk configuration yields the same ordered chunk content
and content hashes.

Retrieved chunks retain source key, version, chunk index and offsets. Context assembly wraps every
chunk as untrusted evidence and explicitly states that instructions inside retrieved content must
not be followed. Security enforcement still remains in code and policy; the prompt boundary is
defense in depth only.

## Deletion and audit

Deleting a source key removes all of its versions, ACL rows and chunks through foreign-key
cascades. Repeating the deletion is a no-op. Successful indexing and deletion write durable
workspace security events.

## Known limits

- This increment does not expose a public ingestion API.
- Embedding generation is not yet connected to a provider adapter.
- Hybrid lexical/vector retrieval, reranking and ANN indexes are follow-up work.
- Retrieval evaluation datasets, recall@k/hit-rate reporting and answer groundedness evaluation
  remain Milestone 5 work.
- Retrieved context is not yet connected to the production model executor.

## References

- https://github.com/pgvector/pgvector
