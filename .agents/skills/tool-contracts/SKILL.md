---
name: tool-contracts
description: Use when defining AI tools/functions, JSON schemas, tool registries, tool adapters, action permissions, retries, or side-effect behavior.
---

# Tool Contracts

Every tool is an API contract exposed to probabilistic callers.

## Contract requirements
- Strong input/output types.
- One responsibility per tool.
- Explicit side-effect classification: read, write, destructive, external-communication.
- Stable error codes separate from human-readable messages.
- Idempotency key for retryable write operations.
- Bounded payload sizes and pagination.

## Agent-facing design
- Tool names should describe intent, not implementation.
- Descriptions must state preconditions and important restrictions.
- Do not expose generic shell, SQL, HTTP, or filesystem escape hatches to normal agents.

## Safety
Run authorization and policy checks immediately before execution because permissions may change after planning.
