# ADR 0007: OpenAI Responses provider adapter boundary

Status: accepted for the first production-capable provider adapter increment.

## Context

Nexora has a provider-neutral routing and adapter contract, but Milestone 5 requires a concrete
provider implementation without coupling worker/domain code to a vendor SDK. Provider credentials,
raw HTTP failures and vendor response objects must not cross the adapter boundary.

## Decision

The first concrete adapter targets the OpenAI Responses API over an injected asynchronous HTTP
client. Production egress is fixed to `https://api.openai.com/v1/responses`; workspace users do
not supply provider URLs, credentials or arbitrary headers.

The adapter:

- accepts credentials as `SecretStr` constructor input rather than model-visible configuration;
- requires an explicit operator-owned model-to-capability map and fails closed for unknown models;
- sends normalized messages, function tools and JSON-schema structured-output requests;
- sets `store=false` on provider requests;
- normalizes text, tool calls, token usage, streaming events and finish state;
- maps HTTP/network failures to stable `ProviderError` codes without persisting provider bodies;
- requires a stable provider `request_id` so active HTTP work can be cancelled;
- preserves required capabilities before any network call;
- keeps vendor response dictionaries inside the adapter and returns only Nexora dataclasses.

## Request correlation and cancellation

Every `ProviderRequest` carries a stable request ID. The adapter registers the current async task
for the lifetime of a generate or stream call. Cancellation is idempotent: cancelling an unknown
or already-finished request is a no-op, while an active request is interrupted and normalized as
`provider_cancelled`.

Duplicate concurrent request IDs are rejected. This prevents one cancellation from accidentally
targeting a different provider operation.

## Errors and retries

Provider error payloads can contain sensitive upstream details, so they are never surfaced through
runtime exceptions. Stable categories are used instead:

- rate limiting, timeouts, selected HTTP conflicts and 5xx failures are retryable;
- authentication, bad request, capability mismatch and invalid provider payloads are terminal;
- transport failures are retryable;
- malformed tool arguments, structured output or stream events fail closed.

The worker/routing layer decides whether and where to retry; the adapter does not silently switch
models or providers.

## Security boundaries

- The API key is never placed in a `ProviderRequest`, run event or tool/model payload.
- Workspace data cannot select an arbitrary provider endpoint, preventing this adapter from
  becoming a generic SSRF primitive.
- Required tool/structured-output/streaming capabilities are checked before egress.
- Tests use `httpx.MockTransport`; CI performs no external model calls and needs no API key.
- The adapter is not automatically registered from environment variables in this increment.

## Known limits

- No production model executor invokes this adapter yet.
- Model capability configuration is operator-supplied; automated provider model discovery is not
  implemented.
- Provider-specific telemetry export and spend accounting remain follow-up work.
- RAG, evaluations and deployment hardening remain open Milestone 5 items.

## References

- https://developers.openai.com/api/docs/guides/function-calling
- https://developers.openai.com/api/docs/guides/structured-outputs
- https://developers.openai.com/api/docs/guides/streaming-responses
