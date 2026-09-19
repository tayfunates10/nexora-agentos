---
name: model-routing
description: Use when implementing multi-model support, provider adapters, model fallback, model selection, capability routing, quotas, or vendor failover.
---

# Model Routing

Provider independence is an architectural requirement.

## Adapter contract
Normalize messages, tools, structured output, streaming, usage, errors and cancellation.

## Routing inputs
Task type, required capabilities, latency target, quality tier, workspace policy, regional constraints, current provider health and budget.

## Rules
- Never silently downgrade a capability required for correctness.
- Fallback must preserve tool/structured-output compatibility.
- Record selected model/provider and routing reason in traces.
- Circuit-break unhealthy providers.
- Enforce per-workspace model allowlists and budgets.
