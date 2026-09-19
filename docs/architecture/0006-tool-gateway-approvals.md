# ADR 0006: Typed tool gateway, policy engine and durable human approval

Status: gateway core, policy and approval workflow implemented; external MCP transport disabled.

## Decision

Nexora treats tools as security-sensitive typed APIs exposed to probabilistic callers. The
`McpGateway` is transport-neutral: future MCP transports may map `tools/list` and tool-call
requests onto it, but this milestone does not claim a network MCP server or remote MCP client.

Every registered tool has a stable name, description, schema version, Pydantic input/output
models, side-effect classification and handler. Generic shell, arbitrary HTTP, SQL and filesystem
escape hatches are not registered. Production currently exposes only the read-only
`run.status.read` tool.

## Policy

Tool policy is evaluated from current database state immediately before execution.

- read -> allow by default
- write -> require approval by default
- destructive -> require approval by default
- external communication -> require approval by default
- unknown tool -> reject

Workspace owners may set an explicit allow, require_approval or deny override for a registered
tool. Admins cannot weaken policy. Owners and admins may approve pending actions.

Membership and the run requester's permission are revalidated from PostgreSQL; token role claims
do not grant tool authority. A run with cancellation requested cannot begin another tool call.

## Durable tool calls

Each tool call persists workspace_id, run_id, actor identity, tool/schema version, side-effect
class, normalized arguments, SHA-256 argument hash, idempotency key, policy result, execution
status, result hash, error code and timing metadata.

The tool-call contract fields and approved arguments are immutable at the database layer. Tool
history cannot be deleted or truncated through normal application writes. Write-class tools must
declare idempotency support before registration.

Tool finalization is fenced by both the active run lease and the matching worker job receipt. A
stale or replaced worker cannot record a later tool result.

## Human approval

When policy requires approval, the gateway atomically:

1. persists the tool call and approval request,
2. records the normalized argument hash,
3. changes the run from running to waiting_for_approval,
4. releases the worker lease,
5. marks the current worker receipt superseded for approval wait,
6. appends an approval-requested event.

The worker recognizes the durable wait state and ACKs the consumed Redis message. No tool handler
runs before approval is committed.

Approval records support pending, approved, rejected, expired and cancelled states. Pending
requests have a bounded TTL configured by `NEXORA_TOOL_APPROVAL_TTL_SECONDS` (default 24 hours).
Before approval, the tool contract and argument hash are validated again.

After a decision, a waiting run returns to queued and a fresh transactional outbox job is
created. On resume, the executor must request the same tool call with the same idempotency key.
Approved calls may then execute; rejected, expired or cancelled calls return their durable state
without executing.

Cancelling the whole run also cancels its pending approval and waiting/approved tool call
atomically.

## Replay and policy changes

Tool execution is at least once at the worker boundary. Durable tool-call idempotency suppresses
normal replay. Production mutation adapters added later must implement real destination-level
idempotency because a remote side effect can succeed even if the local success record is lost.

Policy is re-evaluated before each execution attempt. A previously executing idempotent tool may
be moved back to approval wait or denied if workspace policy becomes stricter before replay.

## Trust boundaries

PostgreSQL is authoritative for membership, policy, approvals, tool calls and run state. Redis is
delivery infrastructure only. The LLM never grants authorization and never bypasses policy or
approval by prompt text.

Tool descriptions and schemas are treated as prompt surface. Tool handlers receive only scoped
execution context and validated arguments. Raw credentials are not exposed as tool results or
stored in run events.

## Remaining work

A future milestone may add an actual MCP protocol transport/client layer and authenticated remote
MCP server registration. That layer must preserve this gateway's schema validation, tenant
scoping, timeouts, cancellation, audit, idempotency and approval checks rather than bypass them.

Production model/provider adapters are also still disabled.

## Skills applied

mcp-server-development, tool-contracts, human-approval, security-threat-modeling,
agent-architecture, api-openapi, testing-quality.
