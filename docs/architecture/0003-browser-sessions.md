# ADR 0003: OIDC browser login and server-side sessions

Status: implemented; production identity-provider credentials must be configured by the operator.

## Browser boundary
The web app is a confidential OIDC client using authorization code flow, S256 PKCE,
state, nonce and ID-token signature verification through openid-client. The callback
also calls the API's /me endpoint with the access token and checks that the two verified
identities agree. The ID token audience is the web client; the access token audience is
Nexora's API. No token minting or auth bypass exists in application code.

Login starts through a same-origin POST. The browser receives only a random 256-bit
flow cookie; verifier/state/nonce live in Redis for ten minutes. Callback consumes this
record atomically with GETDEL, including failed attempts. A fresh session ID is issued
on success and the prior Nexora session is deleted. Return URLs are fixed application
paths built from configured NEXORA_WEB_ORIGIN, never Host or forwarded-host input.

## Sessions
A random 256-bit opaque ID is stored in an HttpOnly, SameSite=Lax cookie. HTTPS
origins use Secure and the __Host- prefix, path / and no Domain attribute. HTTP is only
accepted for localhost/127.0.0.1 during development. Redis keys hash the cookie ID and
use the nexora:web namespace. Access tokens stay on the server, never browser storage,
HTML, or client component props. TTL is at most one hour and never exceeds the provider's
reported access-token lifetime. The API still validates token expiry on every operation.
No refresh tokens are requested or stored; expiry requires another login.

Redis stores access tokens, so it is sensitive infrastructure. Use a dedicated Redis ACL,
TLS (rediss), network restrictions, encrypted storage and protected backups in production.
Local Compose intentionally uses local development Redis. Redis outage fails closed;
connect/command timeouts are bounded. Logout is a same-origin CSRF-protected POST and
deletes server state; replaying an old session cookie cannot restore access. This is
local application logout: it does not sign the user out of their identity provider.

## Workspaces
Server Components load current session and call the API. Forms use POST handlers with
exact Origin and synchronizer-token CSRF checks, bounded URL-encoded request bodies,
Zod validation and fixed API paths. The UI exposes rename to owners/admins and team
access to owners; the API independently enforces membership on every request, including
when permissions change after a page rendered. Error, unavailable, empty, read-only and
expired-session states are explicit. POST creation is not automatically retried.

## Testing and remaining work
A test-only issuer generates RSA-signed ID/access tokens, serves discovery/JWKS, and
verifies code/PKCE exchange. Tests exercise state/nonce/replay defenses and API identity
matching. Browser tests use this issuer, an API fixture and real Redis; actual API
Postgres/RBAC integration remains in the Python CI job. Test fixtures are outside app
routes and are never a deployment auth mode. Browser tests cover sign-in, creation,
rename, membership form, CSRF denial, owner/member UI, viewport overflow and logout replay.

Real identity-provider registration is external configuration, not completed by this PR.
Automated key rotation for the API, provider logout/revocation, distributed login rate
limits and operational session auditing remain production-hardening work. The provider
is responsible for MFA, credential management and provider login audit records.

References:
- https://github.com/panva/openid-client
- https://nextjs.org/docs/app/api-reference/functions/cookies
