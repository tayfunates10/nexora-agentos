---
name: redis-jobs
description: Use when implementing Redis caching, distributed locks, rate limits, queues, worker jobs, ephemeral state, or retry scheduling.
---

# Redis, Queues & Jobs

Redis is ephemeral infrastructure unless explicitly configured otherwise.

## Cache rules
- Define ownership, TTL and invalidation for every cache.
- Never cache authorization decisions beyond safe policy windows.
- Use namespaced keys including workspace where relevant.

## Jobs
- Jobs must be idempotent or carry idempotency keys.
- Use bounded exponential backoff with jitter.
- Separate retryable and terminal failures.
- Dead-letter exhausted jobs with diagnostic context.
- Large payloads belong in durable/object storage; queue references instead.

## Locks
Use locks only when necessary and always with expiration and ownership tokens.
