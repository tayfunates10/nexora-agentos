---
name: postgres-data-modeling
description: Use when designing PostgreSQL schemas, migrations, indexes, tenancy, audit tables, JSONB, pgvector, transactions, or query plans.
---

# PostgreSQL Data Modeling

Schema design must encode integrity, not merely store JSON.

## Rules
- Use database constraints for invariants where practical.
- Every tenant-owned row must have explicit workspace/tenant identity.
- Index according to real query shapes.
- Prefer normalized relational data; use JSONB for genuinely flexible attributes.
- Use timestamptz for real timestamps.
- Plan zero/low-downtime migrations.
- Audit/security records should be append-oriented.

## pgvector
Choose metric and index deliberately, benchmark recall/latency, and keep metadata filters compatible with retrieval patterns.

## Review
Use EXPLAIN ANALYZE for important queries before optimizing by intuition.
