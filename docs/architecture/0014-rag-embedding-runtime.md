# ADR 0014: Operator-configured embedding retrieval in the worker

Status: accepted.

## Context

The RAG foundation stores deterministic chunks and applies workspace membership and source ACL
filters in PostgreSQL, while the durable executor already has an optional retrieval boundary.
The worker did not previously construct a retriever, so queued runs could not use that knowledge
path. The earlier embedding prototype also predated the production worker, observability and
deployment increments.

## Decision

The worker may opt into retrieval through an operator-owned `retrieval` block in its runtime
configuration. Retrieval is disabled when that block is absent.

The first embedding adapter targets the fixed OpenAI `/v1/embeddings` endpoint. Credentials stay
in `NEXORA_OPENAI_API_KEY`; a run, workspace member, document or model response cannot select an
endpoint, API key, embedding model or vector dimension. The configured model and dimension pair is
allowlisted in the adapter before any network request.

For both indexing and retrieval, repository authorization is checked before provider egress. The
repository repeats authorization inside the database operation, and retrieval SQL applies current
workspace membership plus source ACL filtering before any chunk can reach model context.

The executor receives retrieved text only through the existing retriever contract. Context is
wrapped with the RAG untrusted-evidence delimiter and citation provenance. Retrieved instructions
are data, not executable policy, and retrieved content is never written to telemetry.

## Failure and lifecycle semantics

Provider timeout, transport and rate-limit failures are normalized as embedding provider errors.
The embedding transport is closed during worker shutdown independently from generation adapters.
A missing retrieval configuration leaves worker behavior unchanged.

The worker network policy already allows provider TLS egress and denies private/link-local ranges,
so enabling embeddings does not expand the Kubernetes network boundary.

## Limits

This increment wires query-time retrieval into the worker and supplies the embedding/indexing
pipeline. It does not expose a public document-ingestion HTTP API, add hybrid search/reranking, or
select an ANN index. Exact pgvector cosine retrieval remains the default until recall and latency
are measured on a retrieval evaluation set.

## Verification

Tests cover fixed egress, malformed provider responses, cancellation, authorization before paid
egress, untrusted context construction, retrieval configuration validation and shutdown cleanup.

## Skills applied

rag-engineering, auth-rbac-multitenancy, security-threat-modeling, model-routing,
testing-quality, observability-otel.
