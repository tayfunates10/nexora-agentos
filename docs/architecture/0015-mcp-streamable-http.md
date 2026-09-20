# ADR 0015: Operator-allowlisted MCP 2026-07-28 Streamable HTTP transport

Status: accepted.

## Context

Nexora already governs tool registration, policy, durable approvals, idempotency and result
validation independently from transport. Until this increment, however, the worker accepted only
in-process adapters supplied by code. A production deployment therefore could not call a remote
MCP tool server without a custom worker build.

The current MCP revision is `2026-07-28`. Its HTTP path is stateless: `initialize` sessions are
removed for modern traffic, each request carries its protocol envelope, and Streamable HTTP uses
`MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` request headers.

## Decision

The worker can declare a bounded `mcp_servers` mapping in its operator-owned runtime
configuration. Workspace users still register only a `server_key`; they cannot supply a URL,
transport command or credential.

The first production transport is `streamable_http` with these constraints:

- MCP revision is pinned to `2026-07-28`.
- Only stateless `tools/call` is sent.
- Endpoints must use HTTPS on port 443 and may not contain URL credentials, query strings or
  fragments.
- Localhost and non-global literal IP addresses are rejected before startup.
- Redirects are disabled per request.
- Bearer-token values are read from an operator-selected `NEXORA_MCP_*` environment variable;
  the runtime file contains only that variable name.
- Responses may be direct JSON or `text/event-stream`, are bounded before parsing, and must
  contain the matching JSON-RPC request id.
- A modern response must have `resultType: "complete"`. MRTR `input_required` and Tasks
  extension results fail closed for now.
- Successful `structuredContent` is returned directly to the existing output-schema validator.
  When structured content is absent, only text content blocks are accepted and normalized to a
  bounded `{"text": "..."}` object.
- Tool-reported `isError` results are normalized to a stable terminal code; raw upstream error
  bodies are not persisted or logged.

## Authorization and retry boundary

The existing `McpGateway` remains authoritative. It validates the workspace tool contract,
current requester membership, current policy and any durable approval before selecting an adapter.
The transport cannot bypass those checks.

Adapter failures carry an explicit retry classification into the gateway. Timeouts, transient
transport failures, rate limits and 5xx responses are retryable. Authentication failures,
redirects, malformed protocol responses, unsupported result types and other 4xx failures are
terminal. Upstream bodies are never copied into durable error records.

## Deployment

The worker already has public TLS egress while private, loopback, link-local and carrier-grade NAT
ranges are excluded by NetworkPolicy. This transport does not broaden that egress rule.

Kubernetes may provide MCP bearer tokens through the optional `nexora-mcp-secrets` Secret. The
worker starts without it when no configured server references a token environment variable.

## Limits and follow-up

This increment deliberately does not implement automatic `tools/list` synchronization, OAuth
discovery/refresh, `x-mcp-header` argument mirroring, MRTR input handling, Tasks polling,
resources/prompts or stdio. Those capabilities should be added only with dedicated policy and
conformance tests rather than by turning the worker into a general-purpose proxy.

DNS names are operator-controlled and Kubernetes network policy provides the production private
network boundary. Deployments without an enforcing CNI must provide an equivalent egress control.

## Verification

Tests cover required modern headers/envelope metadata, JSON and SSE responses, bearer-token
placement, response size limits, redirect refusal, private/local literal endpoint rejection,
HTTP failure classification, unsupported modern result types, gateway retry propagation,
runtime configuration validation and Kubernetes secret wiring.

## Skills applied

mcp-server-development, security-threat-modeling, auth-rbac-multitenancy,
testing-quality, docker-kubernetes, observability-otel.

## References

- https://modelcontextprotocol.io/specification/2026-07-28
- https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/tools.mdx
