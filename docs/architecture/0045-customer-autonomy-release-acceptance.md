# ADR 0045: Customer autonomy release acceptance

## Context

The generic staging smoke proves identity, worker pickup, model execution, retrieval, approvals and
observability, but it does not prove the customer promise introduced by the standard-agent runtime:
inspect real permitted state, create durable work, execute an approved external mutation and verify
the resulting state.

Issue #51 therefore needs a release gate that fails when an agent merely writes a convincing report
without leaving durable action and verification evidence.

## Decision

Keep the generic `Staging E2E Smoke` unchanged as the low-cost deployment gate and add the manually
triggered `Customer Autonomy Release Smoke` for customer-release acceptance.

The shared `scripts/staging_smoke.py` runner can now target either a custom agent or an installed
standard tenant agent. Optional release assertions inspect the public run actions and run tasks APIs
after the run succeeds. The runner never prints prompts, model output, approval arguments, browser
content or credentials.

The customer gate proves four scenarios:

1. **SEO** — the installed SEO agent creates an approved WordPress draft through
   `wordpress.posts.create`, records a follow-up task and closes that task only after persisted
   verification evidence.
2. **Social** — the installed social agent performs an approved
   `instagram.comments.reply`, records a follow-up and verifies the affected provider state.
3. **Reporting** — the reporting agent persists at least one follow-up through
   `nexora.tasks.create` while the gate rejects any successful non-read external mutation.
4. **Browser** — an installed browser-capable agent successfully calls `browser.page.inspect`
   against its snapshotted allowlisted origin and the final result contains a staging-only sentinel
   from that page.

## Durable evidence requirements

An expected tool must have a persisted run action whose status is `succeeded`. A verified release
task must be a follow-up with status `succeeded`, verification state `verified`, and at least one
satisfied evidence record. For mutating SEO/social scenarios, the verified task must name the exact
external action tool.

The reporting scenario permits internal `nexora.tasks.*` writes because those writes are the
durable plan itself. Any other successful non-read action fails the read-only release scenario.

## Browser SSRF boundary

Browser host validation now resolves the configured HTTPS origin, rejects private/link-local/mixed
public-private answers and pins the validated address into a fresh Chromium process using host
resolver rules. The page URL retains the original hostname for TLS and origin checks, while the TCP
resolution cannot silently change after validation. Each request still receives a fresh browser
context and browser process, so cookies and storage cannot cross tool calls.

## Staging fixture

The protected `staging` GitHub environment must already contain safe, disposable fixtures. The
release workflow does not create customer accounts or insert secrets.

Common variables:

- `NEXORA_STAGING_API_URL`
- `NEXORA_STAGING_WORKER_ADMIN_URL`
- `NEXORA_STAGING_WORKSPACE_ID`

Scenario variables:

- `NEXORA_STAGING_SEO_AGENT_ID`
- `NEXORA_STAGING_SEO_ACTION_PROMPT`
- `NEXORA_STAGING_SOCIAL_AGENT_ID`
- `NEXORA_STAGING_SOCIAL_ACTION_PROMPT`
- `NEXORA_STAGING_REPORTING_AGENT_ID`
- `NEXORA_STAGING_REPORTING_ACTION_PROMPT`
- `NEXORA_STAGING_BROWSER_AGENT_ID`
- `NEXORA_STAGING_BROWSER_PROMPT`
- `NEXORA_STAGING_BROWSER_EXPECT_TEXT`

Secrets remain:

- `NEXORA_STAGING_ACCESS_TOKEN`
- `NEXORA_STAGING_METRICS_TOKEN`

The WordPress and Instagram integrations referenced by the installed staging agents must point to
test accounts where the configured mutations are safe to repeat or are otherwise disposable. The
browser agent must have a snapshotted public HTTPS origin and the sentinel must not be a secret.

## Release rule

Platform CI being green proves the contracts and deterministic regression coverage. A customer
release additionally requires a green `Customer Autonomy Release Smoke` against the protected
staging environment. If fixture variables, provider credentials or disposable provider accounts are
not configured, the workflow must fail rather than silently downgrade the gate.

## Skills applied

security-threat-modeling, human-approval, tool-contracts, testing-quality, cicd-release,
event-driven-workflows, nextjs-frontend.
