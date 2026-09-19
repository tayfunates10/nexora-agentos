---
name: api-openapi
description: Use when designing REST APIs, OpenAPI schemas, pagination, versioning, error envelopes, idempotency, webhooks, or public API contracts.
---

# API & OpenAPI

Design contract-first for externally consumed interfaces.

## Conventions
- Resource-oriented URLs and correct HTTP semantics.
- Stable error envelope with machine-readable code.
- Cursor pagination for large mutable collections.
- Idempotency keys for externally retried mutations.
- Explicit API version strategy.
- OpenAPI examples for non-trivial payloads.

## Compatibility
Avoid breaking response/schema changes. Deprecate before removal and document migration.
