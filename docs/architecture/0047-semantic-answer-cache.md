# ADR 0047: Tenant-scoped semantic answer cache

Status: implemented.

## Context

The exact answer cache introduced in ADR 0046 avoids repeated model calls when a customer asks the
same normalized question. It does not help with paraphrases such as "Kargo kaç günde gelir?" and
"Sipariş teslim süresi nedir?". Re-running the full model for such repeats wastes latency and
provider spend.

Semantic reuse must not weaken Nexora's tenant boundary or replay stale external state.

## Decision

Nexora adds an optional semantic fallback after the exact cache misses.

The operator configures `answer_cache_semantic` with an embedding provider/model, dimensions,
similarity threshold, timeout and optional accounting price. The default threshold is 0.94 and the
example configuration uses `text-embedding-3-small` with 256 dimensions.

Execution order is:

1. Refresh any configured RAG retrieval context.
2. Try the exact tenant/agent answer-cache key.
3. On an exact miss, embed the normalized question and search only the current cache scope.
4. Reuse the closest answer only when cosine similarity is at or above the configured threshold.
5. Otherwise call the normal model and store the new answer plus its query embedding.

Exact hits never make an embedding call.

## Cache scope

Semantic candidates must match all of:

- workspace id;
- agent id and agent kind;
- model profile;
- routed provider and model;
- SHA-256 of the current agent instructions;
- the fresh retrieval-context SHA-256 when RAG is configured;
- semantic embedding model and dimensions;
- normal answer-cache TTL.

The database query also filters by workspace and agent before vector distance is considered. There
is no global or cross-tenant vector search.

## Dynamic data

Runs with advertised external tools remain uncached. Connector, browser, task and MCP actions can
change independently of Nexora and must execute against current state.

RAG is treated differently: retrieval is performed before the cache lookup on every new run. The
exact retrieved evidence text is hashed into the cache scope. A changed source, version, chunk set
or evidence text therefore causes a miss. Nexora may reuse the model answer only after it has
confirmed that the fresh retrieval context is unchanged.

## Spend and failure behavior

Semantic lookup embeddings are provider egress. When accounting is enabled, their price must be
declared and their spend is recorded under the embedding category with a deterministic per-run
source key. Budget authorization happens before the embedding request.

A provider error from the semantic embedding service does not block the customer's run; Nexora
falls back to the normal model path. Accounting/database failures still fail closed rather than
silently losing spend records.

A semantic cache hit writes a normal immutable model-step journal row with routing reason
`workspace_semantic_answer_cache`, zero model input/output tokens and no new model spend. The
embedding lookup cost, if priced, remains visible in the embedding spend ledger.

## Storage

Migration 020 extends `workspace_answer_cache` with the cache scope, normalized question,
embedding model/dimensions and pgvector embedding. Existing exact-cache rows remain valid; semantic
columns are nullable so the migration does not rewrite historical rows.

## Verification

Unit coverage verifies paraphrase reuse, tenant isolation, tool exclusion, retrieval-context
invalidation and embedding-provider fallback. Integration coverage uses PostgreSQL + pgvector to
prove that a paraphrase returns the cached response while the LLM provider is called only once.

Metrics expose exact and semantic cache hit/miss/error outcomes through
`nexora_answer_cache_events_total`.

Skills: agent-architecture, postgres-data-modeling, cost-performance, testing-quality,
security-threat-modeling.
