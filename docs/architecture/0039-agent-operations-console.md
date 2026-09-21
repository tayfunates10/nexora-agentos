# ADR 0039: Agent and run operations console

Status: implemented.

## Context

The platform could define agents, queue durable runs, record append-only run events and publish
requester-scoped results, but only through the HTTP API. The browser console covered sign-in,
workspaces, evaluation history and spend. That left the platform's central workflow — define an
agent, start a run, watch it, read the answer, stop it — reachable only with a bearer token and
a terminal, which is not a usable product and pushes operators toward long-lived tokens in
scripts.

## Decision

Three server-rendered pages under the existing workspace layout:

- `/workspaces/{id}/agents` lists agent definitions with their model profile and instructions,
  carries the create form for owners and admins, and offers a start-run form on each agent for
  every member. The start form is a disclosure on the agent it belongs to, so a run is never
  started against an agent the operator did not mean to pick.
- `/workspaces/{id}/runs` is the history from ADR 0038: status, agent, attempts, duration, with
  status, "started by me" filters and keyset pagination.
- `/workspaces/{id}/runs/{runId}` shows one run: status with plain-language meaning, identifiers
  including the trace id, the append-only event timeline, the result, and cancellation.

Mutations are HTML form posts to route handlers (`/workspaces/agents/create`,
`/workspaces/runs/start`, `/workspaces/runs/cancel`) that revalidate the session, check the CSRF
token and origin the way existing handlers do, validate input against the same bounds the API
enforces, and redirect. The browser never receives the API access token; it stays in the Redis
session.

Run creation requires an idempotency key. The page mints a UUID per render and carries it in a
hidden field, so a double submit or a refresh replays one key and returns the existing run
instead of starting a second. The key is client-supplied and therefore only a key: the API scopes
it to the verified issuer and subject, so replaying someone else's key is not reachable.

Authorization is displayed, never enforced, in the browser. A member sees the read-only notice
and no create form, but the API remains the only thing that decides; every page also renders
403/404 as "not found or access denied" rather than implying the resource exists.

The result section models four distinct states instead of one error: no result yet (the run is
not terminal), no publishable result (409 — the run ended without a complete journal), not
available to you (404 — the caller is not the original requester, and workspace admin does not
override that), and the result itself. A result that carries an answer for a failed or cancelled
run is rejected by the response contract before it renders.

Run events are rendered as an ordered timeline with a known-label lookup that falls back to the
raw event type, so an event a future worker adds is still shown rather than silently dropped.
Payload values are printed as text with `request_id` removed and each value truncated; the
timeline never turns worker-written payload content into markup or a link.

## State and trust boundaries

The console adds no API surface of its own and no new trust boundary: every page and handler
calls the same versioned endpoints with the signed-in user's token. Agent instructions and run
input are tenant content and are rendered as text. The console does not hide a paused run: a run
waiting for approval says so and links to where the decision is made.

Status is a point-in-time read with an explicit refresh link rather than an automatic page
refresh, so the page never re-renders under a reader without their action.

## Verification

Unit tests cover the agent, run, event and result contracts, including the rule that a terminal
status and a finish time imply each other, that a non-successful result carries no answer, that
an unknown event type still renders, and that payload rendering strips request ids, truncates
long values and leaves markup inert. The browser flow creates an agent, starts a run from it,
asserts the queued state and timeline, cancels it and asserts that no partial answer appears,
then asserts a successful run's answer and model steps, that the same run is hidden when the
caller is not its requester, the history filters, an invalid filter, member read-only behaviour
and both viewport widths.

Skills: nextjs-frontend, agent-architecture, auth-rbac-multitenancy, api-openapi,
human-approval, testing-quality, security-threat-modeling.
