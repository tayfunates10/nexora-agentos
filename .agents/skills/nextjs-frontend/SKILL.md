---
name: nextjs-frontend
description: Use when building Nexora web UI with Next.js, React, TypeScript, App Router, server/client components, dashboards, forms, streaming, or accessibility.
---

# Next.js Frontend

Build an accessible enterprise control plane.

## Rules
- Default to Server Components; use Client Components only for interactive/browser state.
- Keep server secrets and privileged data access out of client bundles.
- Validate forms on both client and server.
- Model loading, empty, error, forbidden and partial states explicitly.
- Use typed API clients/contracts.
- Avoid global client state when URL/server state is sufficient.

## Agent UX
Show run status, tool activity, approvals, errors and traceability clearly.
Never hide a pending approval or destructive action behind ambiguous UI.
