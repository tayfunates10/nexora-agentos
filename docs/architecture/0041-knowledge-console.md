# ADR 0041: Knowledge source console

Status: implemented.

## Context

Retrieval was operable only through the API. Adding a document meant a POST with an idempotency
key, and checking whether it had been indexed meant polling an ingestion job by identifier. An
operator could not see what an agent was allowed to retrieve, which made the ACL boundary — the
thing that decides whose evidence ends up in whose answer — invisible in the product.

Ingestion is also where a workspace quietly spends money: embedding runs in the worker and is
metered against the budget. A console that hides the queue hides that too.

## Decision

`/workspaces/{id}/knowledge` lists indexed source versions with their key, version, chunk count
and access scope, and carries a create form for owners and admins. Creating posts to a route
handler that validates the same bounds as the API, mints one idempotency key per render, and
redirects to the page with the queued job identifier so the operator lands on the job rather than
on a list that has not changed yet.

The job panel states which of the five states the job is in and what each means: queued says the
text is stored and no provider has been called, running says embedding is under way and metered,
succeeded reports chunks and embedding tokens, failed names its code, cancelled explains that
deleting the source closes queued work. The response contract enforces the same thing it renders:
an indexed source identifier exists exactly when the job succeeded, and a failure always names a
code.

Deletion is a form on each row, posting to a handler that calls the 204 endpoint through the
shared API client. A source whose ingestion is currently running returns 409, which the console
reports as "being ingested right now" rather than as a generic failure, because the API
deliberately refuses to race the worker there.

The create form offers workspace scope only. Restricted sources need exact issuer/subject pairs
for every account allowed to retrieve them, which is an integration-time list rather than
something to retype into a browser form; the page says so and points at the API. A restricted
source created that way is listed here with its scope, so the console never misrepresents who can
retrieve what.

The reader stays honest about the two-process split: the API stores text and queues work, the
worker embeds. The page says that retrieval is off unless a retrieval-enabled worker is running,
so an empty index is never mistaken for a broken upload.

## State and trust boundaries

Source text is tenant content, submitted through the same CSRF-checked, origin-checked, bounded
form path as every other mutation, with an explicit larger body bound for documents. It is
rendered nowhere in the console: the list shows metadata only, so an indexed document cannot
become a stored injection vector against the operator reading the page. Authorization is the
API's: a member reads the list and sees no create or delete control, and the API refuses those
calls regardless of what the page renders.

Only UTF-8 text and Markdown are accepted, matching the API. PDFs and other binary formats stay
out until there is a parser sandbox for them.

## Verification

Contract tests cover the indexed source shape, the succeeded-implies-source-id and
failed-implies-error-code invariants, the source key character set, the text bound and the scope
enum. The browser flow queues an ingestion, asserts the queued explanation, renders a failed job
with its code, lists an indexed source, deletes it, and checks that a member can read the list but
gets neither control.

Skills: rag-engineering, nextjs-frontend, auth-rbac-multitenancy, api-openapi, cost-performance,
security-threat-modeling, testing-quality.
