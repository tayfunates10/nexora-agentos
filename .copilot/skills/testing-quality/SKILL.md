---
name: testing-quality
description: Use when writing unit, integration, contract, end-to-end, security, regression, or reliability tests for Nexora.
---

# Testing & Quality

Use the test pyramid plus AI evals.

## Required layers
- Unit: domain/policy/business logic.
- Integration: PostgreSQL, Redis, MCP adapters, provider adapters.
- Contract: tool schemas and public API.
- E2E: critical user journeys including approval flows.
- Security: tenant isolation and authorization failures.
- AI eval: probabilistic quality.

## Rules
Tests must be deterministic unless explicitly marked as eval/integration.
Do not mock away the contract you intend to verify.
Every bug fix gets a regression test when feasible.
