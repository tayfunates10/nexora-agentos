# ADR 0016: Durable public knowledge ingestion

Status: proposed and implemented in the ingestion milestone.

## Context

Nexora already has deterministic chunking, tenant-scoped pgvector storage, ACL-filtered
retrieval, fixed-egress embedding adapters and a worker-side retrieval pipeline. The missing
piece is a public ingestion contract. Calling an embedding provider directly from the API
would violate the production network boundary because the API deployment intentionally has no
external egress and does not hold model-provider credentials.

## Decision

The public API accepts bounded UTF-8 text/Markdown source content and persists a durable
`knowledge_ingestion_jobs` record in PostgreSQL. The request contains source key, version,
title, access scope, optional ACL identities, metadata and deterministic chunk parameters.
The API never embeds the content and never places raw source text on Redis.

A retrieval-enabled worker polls the durable job table, claims at most one job under a bounded
lease, rechecks the requester's current `knowledge:manage` permission and then calls the
existing `RagEmbeddingPipeline`. The same operator-managed retrieval configuration controls
embedding provider, model, dimensions, batching and timeout for both query-time retrieval and
ingestion.

## API contract

`POST /api/v1/workspaces/{workspace_id}/knowledge/sources` requires an
`Idempotency-Key`. The key is scoped to workspace plus requesting identity. Repeating the same
normalized request returns the existing job; reusing the key for different content returns HTTP
409.

The API also exposes job status, ACL-filtered current source listing and source deletion. Job
responses never echo source text.

Only owners and admins can queue ingestion or inspect ingestion job state. Current workspace
members can list sources, but restricted source metadata is returned only when the member's
issuer/subject appears in the source ACL.

## Worker reliability

Job states are `queued -> running -> succeeded|failed`, with `running -> queued` for bounded
retry and `queued|running -> cancelled` for administrative cancellation paths. PostgreSQL
enforces the legal transitions.

A worker claim increments `attempt_count`, assigns a bounded lease and renews that lease while
embedding/indexing is in progress. Completion is fenced by the current lease owner and expiry.
Expired running jobs may be reclaimed. Retryable embedding/provider failures use bounded
exponential backoff with deterministic jitter and stop after three attempts.

Authorization is checked immediately before paid embedding work. If the requester's role was
removed or demoted after enqueue, the job fails with a stable permission-revoked error and no
embedding request is sent.

## Deletion and races

Deleting a source key cancels queued ingestion jobs for that key before deleting indexed source
versions. Deletion returns HTTP 409 while an ingestion job for the same source key is actively
running. This deliberately avoids the race where an in-flight worker could recreate a source
after a successful delete response.

## Security boundaries

Raw source content stays in PostgreSQL and is never copied into Redis, telemetry, job-stream
payloads or API status responses. Source ACL filtering remains enforced in SQL before retrieved
content reaches a model. Retrieved content remains untrusted evidence.

The first public ingestion contract accepts text/Markdown only. PDF, Office documents, HTML
fetching and other binary or remotely fetched formats require a separate parser/sandbox boundary;
this milestone does not add arbitrary URL fetching or unsafe file parsing.

## Known limits

Hybrid lexical/vector retrieval, reranking and ANN indexing remain future quality/performance
work and should be selected from measured retrieval evaluations. Large-file multipart upload and
object-storage staging are not included in this increment.

## Skills applied

rag-engineering, api-openapi, fastapi-backend, postgres-data-modeling,
security-threat-modeling, testing-quality.
