---
name: human-approval
description: Use when implementing human-in-the-loop approvals, confirmations, policy gates, destructive actions, external communications, or resumable agent execution.
---

# Human Approval

Approval is a durable workflow state, not a modal dialog.

## Require approval for
- Destructive mutations.
- Sending external communications when policy requires it.
- Financial or permission-changing actions.
- High-risk tools configured by workspace policy.

## Approval record
Persist run_id, tool_call_id, requested action, normalized arguments, requester, approver, status, timestamps and policy reason.

## Rules
- Never execute before approval is committed.
- Revalidate authorization and arguments after approval and before execution.
- Approved content must be immutable; material argument changes require a new approval.
- Support approve, reject, expire and cancel.
- Resume execution idempotently after approval.
