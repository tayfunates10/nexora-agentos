# ADR 0009: Durable bounded model executor

The executor uses operator-managed model profiles and workspace-scoped tool policy.
A run cannot choose credentials, network endpoints, or an unconfigured model.

## State and recovery
Each numbered model response is committed before any requested tool executes. On retry
or human approval resume, persisted responses are replayed, while the MCP gateway
reuses stable run/step/tool keys and its durable tool result. Tool arguments (including
mutation idempotency keys) are therefore unchanged across retries. Only the next
uncommitted model step can be generated again. A provider request whose response was
lost before commit may be billed again; model calls are not claimed to be exactly-once.
Remote mutations must honor their idempotency key.

Every journal operation validates workspace, agent, trace, current job receipt, live lease,
attempt number, cancellation, and current requester membership. Gateway execution and
completion use the same fencing check. A lost lease cannot persist a model response or
finalize a tool belonging to a newer attempt. Context and tool output are never treated
as authorization. The executor accepts only tools advertised for the current workspace.

The loop has hard limits on model turns, calls per turn, output tokens, accumulated
reported tokens, context size, model timeouts, and cancellation polling. An incomplete
model response cannot become a successful run. Provider errors retain retry semantics;
unexpected failures remain normalized by the worker. Responses and usage are durable;
event payloads contain metadata rather than prompts or tool results.

## Deployment boundary
The worker is an opt-in Compose service. Operator profiles explicitly map allowed
workspace IDs to model names and enabled tool names. No profile means no execution.
API keys remain environment secrets. Tests use deterministic provider/transport doubles
and real PostgreSQL/Redis in CI; these are not evidence of model answer quality.
