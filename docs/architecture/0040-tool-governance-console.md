# ADR 0040: Tool governance and approval console

Status: implemented.

## Context

Tool governance was complete in the API and invisible in the browser. A run that reached a
destructive tool paused on a durable approval, released its worker lease and waited — and the
only way to see or decide that was an authenticated POST. Human-in-the-loop approval that needs
a terminal is not human-in-the-loop in practice: the call sits until it expires, and the run
fails for a reason nobody watched happen.

Two gaps blocked a console. Approvals could only be listed in full, so the open decisions were
buried behind every closed one in a busy workspace. And the tool listing carried the contract but
not its policy, so a page could show what a tool is without showing whether it may be called.

## Decision

Two small API additions, both read-side:

- `GET /workspaces/{id}/approvals` accepts `status`, validated against the approval state enum.
  Expiry is still swept before the read, so a decision page never offers a request that has
  already lapsed.
- `GET /workspaces/{id}/tools` returns `policy_decision`, `policy_reason` and
  `policy_updated_at` alongside each contract. A tool with no policy row returns nulls, which the
  console renders as denied. Absence is never reported as an allow.

Two pages:

- `/workspaces/{id}/approvals` lists approvals by status, pending first by default. Each card
  shows the tool, the policy reason that held the call, the requester, a link to the run, the
  time left before expiry, and the normalized arguments pretty-printed as inert text. Approve and
  reject are two buttons in one form; the decision posts to a route handler with the same CSRF
  and origin checks as every other mutation. Reading and deciding both need `tool:approve`, so a
  member sees the access-denied state rather than an empty list.
- `/workspaces/{id}/tools` lists each contract with its side effect, server key, enabled state,
  input schema and current policy, plus a policy form for owners and admins. Where an allow
  cannot remove the human step — destructive tools and tools that send external communication —
  the page says so next to the policy rather than implying the call will run unattended.

The console never re-decides anything locally. A second decision on a closed approval is refused
by the API and surfaced as "no longer open" instead of a blank failure, which is also what a
stale tab gets after an approval expires or its run is cancelled.

## State and trust boundaries

No new authorization exists here: the pages call the same endpoints with the user's token and the
API decides. Approval arguments are model-proposed values and are rendered as text inside a
`pre`, never as markup, a link or a form default. The console cannot register a tool contract,
because a contract carries a JSON schema and a server key, and it cannot name a URL, command or
credential — the operator maps a server key to a real endpoint in the worker runtime file, which
stays outside anything a workspace can edit.

Approving is still not executing. The gateway revalidates membership, the current policy and the
tool contract hash after the decision, so a contract changed between request and approval
invalidates the approval rather than running under it.

## Verification

Integration tests cover the policy fields on the tool listing including a registered tool with no
policy, the approval status filter for open and closed states, rejection of an invalid status
value, and that a member can read a policy but not set one. Contract tests cover the whole-or-
absent policy rule, default-deny presentation, the side effects that force approval regardless of
policy, the pending/decided invariant, the submittable decisions, expiry countdown and inert
argument rendering. The browser flow sets a policy, decides a held approval, checks the closed
state and filter, the refusal of a second decision, and member access to both pages.

Skills: human-approval, tool-contracts, nextjs-frontend, auth-rbac-multitenancy,
security-threat-modeling, api-openapi, testing-quality.
