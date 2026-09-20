# ADR 0030: OIDC discovery and automatic JWKS signing-key rotation

Status: implemented for API bearer verification.

## Problem

The API previously required one deployment-managed RSA public PEM. That pins a single signing key,
so a normal identity-provider rotation requires a coordinated API configuration change and restart.
Fetching a key URL from a JWT header would solve rotation incorrectly by turning attacker-controlled
token data into server-side network access.

## Decision

When `NEXORA_AUTH_PUBLIC_KEY` is present, Nexora preserves the existing static RS256 verification
mode for isolated tests and deployments that deliberately pin a key. When it is absent, the API uses
the operator-configured `NEXORA_AUTH_ISSUER` to fetch
`<issuer>/.well-known/openid-configuration`, requires the returned issuer to match exactly, then
loads the provider's RS256 signing keys from `jwks_uri`.

Only HTTPS endpoints without embedded credentials are accepted. Redirects are disabled. Discovery
and JWKS responses are size-bounded, the number of keys is bounded, private RSA material is rejected,
and only RSA signature keys compatible with RS256 are cached. Tokens must carry a bounded `kid`.
The JWT's `jku`, `x5u`, or any other URL-bearing header is ignored and can never select egress.

By default a discovered `jwks_uri` must share the configured issuer origin. Providers that
legitimately publish keys from another origin require the operator to set
`NEXORA_AUTH_JWKS_URL` explicitly. This keeps cross-origin egress an operator decision rather than
metadata expansion.

Keys are cached for five minutes by default. A previously unseen `kid` can force an early JWKS
refresh, rate-limited to once every ten seconds per API process to prevent arbitrary-kid traffic from
turning authentication into an outbound request amplifier. Expired caches fail closed if the provider
cannot be reached; stale keys are not used indefinitely.

## Deployment boundary

The Kubernetes API workload now needs the same public TLS identity-provider egress already required
by the web sign-in service. Private address space remains excluded by the base policy; an internal
identity provider requires an explicit environment-specific NetworkPolicy. The base deployment no
longer requires `NEXORA_AUTH_PUBLIC_KEY` in `nexora-secrets`.

Static public-key mode remains available as a break-glass or intentionally pinned configuration, but
automatic rotation is the repository deployment default.

## Validation

Tests cover issuer-path discovery, cache reuse, unknown-`kid` rotation, rejection of cross-origin
discovery without operator opt-in, explicit cross-origin JWKS, provider outage fail-closed behavior,
RS256/`kid` requirements, and proof that token-supplied `jku` is never contacted.

Skills: auth-rbac-multitenancy, security-threat-modeling, testing-quality, fastapi-backend.
