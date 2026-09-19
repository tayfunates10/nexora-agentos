---
name: mcp-server-development
description: Use when creating or integrating Model Context Protocol servers, resources, prompts, tools, transports, authentication, or MCP client connections.
---

# MCP Server Development

Use MCP as a typed integration boundary.

## Rules
- Tools must have clear names, narrow responsibilities and JSON-schema-compatible inputs.
- Validate every argument server-side even if the client validated it.
- Distinguish read-only tools from mutation tools.
- Mutation tools require authorization and may require human approval.
- Return structured, minimal results; avoid leaking credentials or unrelated records.
- Add timeouts, cancellation and bounded retries to remote operations.
- Treat tool descriptions as security-sensitive prompt surface.

## Security
- Never pass raw secrets into model-visible content.
- Enforce tenant/workspace scoping inside the MCP server, not only in the UI.
- Record tool name, actor, inputs hash, policy result, execution status and duration.
- Reject unknown fields for privileged operations when practical.

## Testing
Provide contract tests for schemas, auth failures, tenant isolation, timeouts and idempotent retries.
