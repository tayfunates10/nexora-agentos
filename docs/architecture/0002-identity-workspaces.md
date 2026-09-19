# ADR 0002: Verified identities and tenant-scoped workspace operations

Status: implemented API milestone; browser login and identity-provider provisioning remain pending.

## Assumptions and threat model
Only human-user access tokens issued for Nexora's dedicated API audience are supported.
The deployment supplies an issuer, audience and RSA public key. RS256 is fixed by server
policy; exp, iat, iss, aud and sub are mandatory. Unsigned, expired, wrong-audience,
wrong-issuer and wrong-key tokens are rejected. Authorization is never read from JWT
role/workspace claims or client headers. Identity is (issuer, subject), never email.
No signing keys, passwords, token minting endpoint or development auth bypass are shipped.

This is an offline resource-server verifier, not a complete OIDC login implementation.
Deployment must configure the issuer to issue **user access tokens** for this audience;
ID tokens and machine credentials must use distinct audiences. Public-key rotation is
manual and requires restart. JWKS refresh, revocation, provider login/logout, browser
sessions and MFA remain future work. An unset auth configuration fails closed (503).
A bearer token is replayable until expiry; require short-lived access tokens and TLS.

## Authorization and isolation
Owner can read, rename and assign admin/member roles. Admin can read and rename.
Member can read. Unknown roles have no grants. Non-members receive 404, concealing
workspace existence. Memberships are loaded from PostgreSQL on each operation.
All workspace data queries include verified issuer/subject or follow a locked membership
check in the same transaction. All data SQL is parameterized. The workspace row lock
serializes membership changes against writes; revoked privileges do not survive caching.

Workspace creation and initial owner membership commit atomically. Only one owner is
allowed by a partial unique index. Owner assignment/demotion is blocked by the API;
owner transfer/deletion is not offered yet. Membership assignment takes a provider subject
under the configured issuer, not an email invitation. No messages or invitations are sent.

## Persistence and audit
Forward-only SQL migrations use a transaction, advisory lock and checksum ledger.
Migration runs are explicit deployment steps. Existing databases are upgraded without
resetting volumes. Create, rename and membership changes append security events in the
same transaction; audit failure rolls back the action. A trigger rejects event mutation.
A database superuser could disable triggers: production must use a separate migration
identity and least-privileged API database role. Compose still uses local development roles.
Authentication happens at the external provider; login auditing is owned by that provider.
No database RLS claim is made; API query scoping is the current isolation boundary.

## API
GET /api/v1/me
POST/GET /api/v1/workspaces
GET/PATCH /api/v1/workspaces/{id}
PUT /api/v1/workspaces/{id}/members

List uses UUID keyset pagination (limit 1–100). POST creation is not idempotent; callers
must not automatically retry it after an uncertain network response. Tokens and their
claims are never logged or echoed in error envelopes. 401 preserves WWW-Authenticate.

## Applied skills
Auth/RBAC/multitenancy, Postgres data modeling, security threat modeling, FastAPI,
API/OpenAPI and testing-quality.

## Reference
https://pyjwt.readthedocs.io/en/stable/usage.html
