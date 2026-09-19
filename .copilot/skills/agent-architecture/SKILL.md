---
name: agent-architecture
description: Use when designing agent runtimes, agent lifecycle, orchestration, memory, planning, handoffs, tool execution, or agent state in Nexora.
---

# Agent Architecture

Design agents as controlled state machines, not unconstrained chat loops.

## Required boundaries
- Separate agent definition, runtime execution, model adapter, tool registry, policy engine, memory, and trace storage.
- Persist execution state so runs can resume after worker/process failure.
- Give every run a stable run_id, workspace_id, agent_id, user_id and trace_id.
- Make retries explicit and idempotent.
- Never let the model directly execute privileged side effects.

## Runtime flow
Validate request -> authorize -> load agent config -> resolve model -> retrieve context -> plan/decide -> request tool -> policy check -> optional human approval -> execute -> observe -> evaluate -> persist result.

## Architecture rules
- Domain logic must not depend on a model provider SDK.
- Tool calls use typed schemas and deterministic validation.
- Long-running work belongs in background workers.
- Store model/tool events as append-only execution history.
- Define terminal, failed, waiting-for-approval, cancelled and retryable states.

## Definition of done
Document state transitions, failure modes, retry semantics and trust boundaries before implementing a new runtime feature.
