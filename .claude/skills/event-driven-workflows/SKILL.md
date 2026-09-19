---
name: event-driven-workflows
description: Use when building asynchronous workflows, event buses, durable orchestration, state machines, retries, sagas, webhooks, or agent background execution.
---

# Event-Driven Workflows

Use events for decoupling, not as a substitute for domain clarity.

## Events
Name facts in past tense and version payload contracts.
Include event_id, occurred_at, aggregate/workspace identifiers and trace context.

## Consumers
Must be idempotent. Persist deduplication for side-effecting consumers where required.

## Workflows
Persist workflow state, retry policy, deadlines and compensation behavior.
Use an outbox/inbox pattern when database state and event publication must stay consistent.

## Webhooks
Verify signatures, reject replays, tolerate retries and process asynchronously.
