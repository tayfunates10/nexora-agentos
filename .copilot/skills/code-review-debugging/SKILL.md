---
name: code-review-debugging
description: Use when reviewing code, investigating bugs, refactoring risky code, diagnosing CI failures, or preparing a change for merge.
---

# Code Review & Debugging

Review for correctness before style.

## Review order
1. Security and tenant isolation.
2. Behavioral correctness and invariants.
3. Failure/retry/idempotency behavior.
4. Data consistency and migrations.
5. API compatibility.
6. Observability and tests.
7. Maintainability.

## Debugging
Reproduce -> narrow scope -> inspect evidence -> form hypothesis -> falsify/test -> fix root cause -> add regression coverage.

Do not hide defects with broad exception catches, arbitrary sleeps, disabled tests or silent fallback.
