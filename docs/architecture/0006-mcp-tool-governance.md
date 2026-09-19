# ADR 0006: Governed MCP tool execution and durable approvals

Status: accepted for the tool-governance milestone.

## Context

The worker state machine can execute provider-independent run logic, but external actions need a
separate trust boundary. MCP tools are probabilistic-caller APIs: their schemas, authorization,
side effects, retry semantics and approval state must be enforced outside the model.

The current MCP specification is the 2026-07-28 revision. Nexora keeps protocol transport behind
an adapter so the governance domain is independent of SDK or transport churn.

## Decision

Nexora introduces a governed MCP gateway with four layers:

1. A workspace-scoped typed tool registry.
2. A default-deny policy record evaluated at call time.
3. Durable tool-call and approval records in PostgreSQL.
4. Operator-provided MCP adapters selected by a bounded `server_key`.

Workspace users never provide raw transport URLs, process commands or credentials to an agent.
This prevents the first implementation from becoming a generic SSRF, shell or credential-passing
escape hatch. A later transport adapter may use the official MCP Python SDK over Streamable HTTP,
but it must preserve the same policy boundary.

## Tool contract

Every registered tool has:

- a stable agent-facing name and remote MCP tool name;
- a strict JSON object input schema with unknown top-level fields forbidden;
- an optional strict output schema;
- a side-effect class: `read`, `write`, `destructive`, or
  `external_communication`;
- an operator adapter key and enabled flag.

Mutating tools must require an `idempotency_key` string in their input schema. Arguments are
canonicalized and SHA-256 hashed before execution. Tool call keys are unique per run so retries
cannot silently change the selected tool or approved arguments.

The initial schema validator intentionally supports a constrained JSON Schema subset. Unsupported
keywords fail registration rather than being silently ignored.

## Policy and authorization

There is no implicit allow. A tool without a policy is denied.

Policies are `allow`, `deny`, or `require_approval`. Destructive and external-communication
tools are always upgraded to `require_approval`, even if an administrator stores `allow`.

Authorization is checked at the API boundary and again immediately before an MCP adapter call.
The original run requester must still be a current workspace member with `agent:run`. The tool
must still be enabled and its current policy must still permit execution.

Owners and admins may manage tools and policies and may approve tool calls. Members can use tools
only indirectly through agent runs.

## Durable approval flow

Approval is workflow state, not a browser confirmation:

`running -> waiting_for_approval -> queued -> running`

A request persists the run, tool call, normalized arguments, arguments hash, requester, policy
reason and expiry time before the worker releases its lease. The original queue delivery is then
acknowledged.

Approval records preserve requested content immutably. Material argument changes require a new
call key and approval. Approving revalidates requester membership, current tool configuration,
schema and policy before a new outbox job is committed. Rejecting or expiring an approval cancels
the run. Cancelling a waiting run cancels its pending approval.

On resume, the gateway sees the same stable call key. An approved call is revalidated immediately
before execution. A succeeded call returns its stored result on replay instead of performing the
side effect twice.

## Retry and failure boundary

Remote adapter timeouts and unavailable adapters are retryable. Policy, schema and approval
violations are terminal. Unknown adapter exceptions are normalized to stable error codes; raw
exceptions are never persisted as tool results.

A worker crash after a remote mutation but before result persistence can cause a replay. For that
reason every non-read tool contract requires an idempotency key and the remote tool is expected to
honor it.

## Audit and observability

Tool registration and policy/approval decisions write security events. Agent run events record
planning, approval requests, waits, approvals, starts, retries, failures and success. Tool-call
rows store bounded normalized inputs, policy decision, attempt count and bounded structured
results.

Credentials are not stored in tool definitions, tool calls, approvals or model-visible events.

## Known limits

- No arbitrary workspace-configured MCP URL or stdio command is supported.
- No production MCP transport adapter is enabled by default.
- Approval expiry is processed when approval state is accessed; a periodic sweeper can be added
  with the worker-service deployment.
- Tool discovery/synchronization from MCP servers is not automatic yet.
- Production egress controls and short-lived server credentials belong to deployment hardening.

## References

- https://modelcontextprotocol.io/specification/2026-07-28
- https://py.sdk.modelcontextprotocol.io/
