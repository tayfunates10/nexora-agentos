# ADR 0034: Retrieval evaluation and model-scoped HNSW acceleration

Status: implemented.

## Context

ADR 0033 added deterministic hybrid lexical/vector ranking, but retrieval quality and vector-query
latency still need separate measurement. The `rag_chunks.embedding` column intentionally stores
multiple embedding models and dimensions in one `vector` column. pgvector can query that shape
exactly, but an HNSW index must target rows with one fixed dimension through an expression and a
partial predicate.

A single global ANN index would either be invalid for mixed dimensions or silently couple unrelated
embedding models. Approximate search also interacts with workspace and ACL filters: filtered rows are
not allowed to leak into context, and post-index filtering must not starve an authorized tenant of
results.

## Decision

Add two independent capabilities.

### Retrieval-only evaluation

`nexora_api.retrieval_eval` scores saved retrieval cases without invoking answer generation. A case
contains a stable id, a query embedding, one or more expected source keys and optional query text.
Reports expose:

- hit rate at K;
- mean recall at K;
- mean reciprocal rank;
- the bounded ranked source keys for every case.

This keeps retrieval regressions separate from LLM judging. CI uses deterministic vectors so the
quality gate does not depend on an external model provider.

### Operator-managed HNSW

HNSW remains opt-in. The worker runtime accepts:

```json
"ann": {
  "ef_search": 100
}
```

The operator first provisions one index for the exact embedding model/dimension pair with the
migration/database-owner credential:

```bash
python -m nexora_api.rag_ann ensure \
  --model text-embedding-3-small \
  --dimensions 1536
```

The index name is deterministic from model plus dimension and the index is built with
`CREATE INDEX CONCURRENTLY`, so it is not embedded in the transactional schema migration runner.
Runtime database roles still cannot create or drop indexes.

The index is a partial expression HNSW index over
`embedding::vector(<dimensions>) vector_cosine_ops`, restricted to the reviewed
`embedding_model` and `embedding_dimensions`. Vector HNSW is rejected above 2000 dimensions.

When ANN is configured, retrieval checks that the expected HNSW index exists, is valid, ready and
uses the expected cosine expression before any paid query embedding leaves the worker. Missing or
invalid infrastructure therefore fails closed instead of silently falling back to a full scan.

Queries set bounded `hnsw.ef_search` and enable `hnsw.iterative_scan=strict_order`. This matters
because workspace/current-version/ACL predicates remain mandatory and approximate indexes apply
filters during scanning. The vector candidate pool uses the HNSW-compatible
`ORDER BY distance LIMIT` shape; deterministic source/chunk tie-breaking happens after that pool.

## Security and tenancy

ANN never changes authorization. Workspace membership and source ACL predicates are applied inside
the vector candidate query before any chunk enters hybrid fusion or context assembly. An HNSW index
contains rows from multiple workspaces for the same embedding model, but the query cannot return a
chunk that fails tenant and ACL predicates.

The operator, not a tenant or model, selects whether ANN is enabled and controls its parameters.
Workspace APIs cannot create indexes, select `ef_search` or name an index.

## Verification

CI covers:

- deterministic hit@K, recall@K and MRR calculations;
- ANN configuration bounds and the 2000-dimension HNSW limit;
- missing-index preflight proving no embedding provider call occurs;
- online creation of a model/dimension-specific HNSW index against PostgreSQL/pgvector;
- `EXPLAIN` proof that the expected partial HNSW index is selected;
- exact-search versus HNSW retrieval evaluation on the same deterministic cases;
- cross-tenant distractors proving ANN does not widen workspace isolation.

These fixtures are a regression gate, not a universal production recall claim. Operators should run
representative retrieval sets for their own corpus before lowering `ef_search` or changing index
construction parameters.

## Rollback

Disable the runtime `ann` block to return immediately to exact vector ranking. After workers no
longer depend on the index, remove it online:

```bash
python -m nexora_api.rag_ann drop \
  --model text-embedding-3-small \
  --dimensions 1536
```

## Skills applied

rag-engineering, postgres-data-modeling, cost-performance, security-threat-modeling,
testing-quality, code-review-debugging.
