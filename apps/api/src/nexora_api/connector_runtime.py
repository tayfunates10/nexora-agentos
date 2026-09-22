"""The layer that holds secrets so agents never have to.

An agent asks for a capability — `instagram.insights.read` — against a binding. The
runtime resolves the binding to a tenant integration, opens that integration's credential
inside this process, builds the outbound request and returns only the response. The
secret is never an argument, never a return value, never a log field and never part of a
prompt.

Outbound requests are constrained by the connector definition: a fixed base URL (or one
the tenant declared for its own system), an absolute path that cannot escape it, no
redirects, a bounded response and a hard timeout.
"""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from nexora_api.integration_manifest import ConnectorManifest, secure_https_url
from nexora_api.logs import context, logger

MAX_RESPONSE_BYTES = 262_144
log = logger("api.connector")


@dataclass(frozen=True)
class ConnectorResult:
    """What a capability call produced. Never carries credential material."""

    ok: bool
    status_code: int | None
    # A stable machine code the console and the run timeline can act on.
    error_code: str | None = None
    body: str | None = None

    @property
    def credential_rejected(self) -> bool:
        return self.status_code in (401, 403)


class ConnectorError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def _resolvable_public_host(host: str, port: int) -> None:
    """Refuse a hostname that resolves onto the platform's own network.

    Tenants supply base URLs for systems Nexora has never seen, so a name pointing at a
    link-local or private address is treated as an attempt to reach inside, not as a
    misconfiguration to follow.
    """
    try:
        records = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror:
        raise ConnectorError("host_unresolvable") from None
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0])
        except ValueError:
            raise ConnectorError("host_unresolvable") from None
        if not address.is_global:
            raise ConnectorError("host_not_routable")


class ConnectorRuntime:
    def __init__(self, timeout_seconds: float = 5.0):
        self.timeout_seconds = timeout_seconds

    def base_url(self, definition: ConnectorManifest, config: dict[str, object]) -> str:
        """The connector's own base URL, or the one the tenant declared for its system."""
        if definition.base_url:
            return definition.base_url.rstrip("/")
        if not definition.base_url_field:
            raise ConnectorError("capability_not_executable")
        declared = config.get(definition.base_url_field)
        if not isinstance(declared, str) or not declared:
            raise ConnectorError("base_url_missing")
        try:
            return secure_https_url(declared.rstrip("/"), "base URL")
        except ValueError:
            raise ConnectorError("base_url_rejected") from None

    @staticmethod
    def _authorize(
        definition: ConnectorManifest, credential: dict[str, str]
    ) -> tuple[dict[str, str], dict[str, str], tuple[str, str] | None]:
        """Build the authenticated parts of a request from the declared placement."""
        placement = definition.credential_placement
        if placement is None:
            raise ConnectorError("capability_not_executable")
        secret = credential.get(placement.value_field)
        if not secret:
            raise ConnectorError("credential_missing")
        if placement.kind == "bearer_header":
            return {"Authorization": "Bearer " + secret}, {}, None
        if placement.kind == "header":
            return {placement.header: secret}, {}, None
        if placement.kind == "query":
            return {}, {placement.query_parameter: secret}, None
        user = credential.get(placement.username_field or "")
        if not user:
            raise ConnectorError("credential_missing")
        return {}, {}, (user, secret)

    async def invoke(
        self,
        definition: ConnectorManifest,
        capability: str,
        credential: dict[str, str],
        config: dict[str, object],
        arguments: dict[str, object] | None = None,
    ) -> ConnectorResult:
        """Perform one capability call without allowing arguments to widen the destination.

        GET/DELETE tools may supply a query object. POST tools may supply a payload
        object. Mutations carry the governed idempotency key as an HTTP header, while the
        key itself is never copied into the provider payload.
        """
        endpoint = definition.endpoint_for(capability)
        if endpoint is None:
            raise ConnectorError("capability_not_executable")
        base = self.base_url(definition, config)
        headers, params, basic = self._authorize(definition, credential)
        supplied = arguments or {}
        query = supplied.get("query", {})
        payload = supplied.get("payload")
        idempotency_key = supplied.get("idempotency_key")
        if not isinstance(query, dict):
            raise ConnectorError("invalid_query_arguments")
        if payload is not None and not isinstance(payload, dict):
            raise ConnectorError("invalid_payload_arguments")
        if endpoint.method != "GET":
            if not isinstance(idempotency_key, str) or not idempotency_key:
                raise ConnectorError("idempotency_key_missing")
            headers["Idempotency-Key"] = idempotency_key
        params.update({str(key): value for key, value in query.items()})

        url = base + endpoint.path
        parsed = urlsplit(url)
        await _resolvable_public_host(parsed.hostname or "", parsed.port or 443)

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                auth=basic,
            ) as client:
                response = await client.request(
                    endpoint.method,
                    url,
                    headers={**headers, "Accept": "application/json"},
                    params=params,
                    json=payload if endpoint.method == "POST" else None,
                )
        except httpx.TimeoutException:
            return ConnectorResult(ok=False, status_code=None, error_code="timeout")
        except httpx.HTTPError:
            return ConnectorResult(ok=False, status_code=None, error_code="transport_error")

        body = response.text[:MAX_RESPONSE_BYTES] if response.content else None
        # The status is diagnostic; the provider's body may echo request detail, so it is
        # bounded and only returned to the caller, never written to a log line.
        log.info(
            "connector call",
            extra=context(
                integration=definition.id,
                capability=capability,
                status=response.status_code,
            ),
        )
        if response.is_success:
            return ConnectorResult(ok=True, status_code=response.status_code, body=body)
        code = {401: "unauthorized", 403: "forbidden", 429: "rate_limited"}.get(
            response.status_code, "provider_error"
        )
        return ConnectorResult(ok=False, status_code=response.status_code, error_code=code)
