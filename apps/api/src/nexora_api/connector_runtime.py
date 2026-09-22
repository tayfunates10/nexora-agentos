"""The layer that holds secrets so agents never have to.

An agent asks for a declared connector capability against a tenant binding. The runtime
resolves the binding to a tenant integration, opens that integration's credential inside
this process, validates agent-supplied arguments against the published contract, maps only
those declared fields onto a fixed provider request and returns a bounded response.

The model never chooses a URL, HTTP method, provider parameter name, credential placement
or retry semantics. Secrets never enter tool arguments, prompts, queue messages, events or
logs.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from nexora_api.integration_manifest import ConnectorManifest, secure_https_url
from nexora_api.logs import context, logger
from nexora_api.tool_contracts import ToolContractError, validate_arguments

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


HostValidator = Callable[[str, int], Awaitable[None]]


class ConnectorRuntime:
    def __init__(
        self,
        timeout_seconds: float = 5.0,
        *,
        client: httpx.AsyncClient | None = None,
        host_validator: HostValidator = _resolvable_public_host,
    ):
        if not 0.1 <= timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0.1 and 120")
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._host_validator = host_validator

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

    @staticmethod
    def _validated_arguments(endpoint, arguments: dict[str, Any] | None) -> dict[str, Any]:
        supplied = arguments or {}
        try:
            validate_arguments(supplied, endpoint.input_schema)
        except ToolContractError as exc:
            raise ConnectorError("connector_invalid_arguments") from exc
        return supplied

    @staticmethod
    def _mapped_request(endpoint, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        query = {
            provider_name: arguments[input_name]
            for provider_name, input_name in endpoint.query_map.items()
            if input_name in arguments
        }
        body = {
            provider_name: arguments[input_name]
            for provider_name, input_name in endpoint.body_map.items()
            if input_name in arguments
        }
        return query, body

    async def _request(self, *, method: str, url: str, headers, params, body, basic):
        request_kwargs = {
            "headers": headers,
            "params": params,
            "json": body or None,
            "auth": basic,
            "timeout": self.timeout_seconds,
            "follow_redirects": False,
        }
        if self._client is not None:
            return await self._client.request(method, url, **request_kwargs)
        async with httpx.AsyncClient() as client:
            return await client.request(method, url, **request_kwargs)

    async def invoke(
        self,
        definition: ConnectorManifest,
        capability: str,
        credential: dict[str, str],
        config: dict[str, object],
        *,
        arguments: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> ConnectorResult:
        """Perform one declared capability call after deterministic validation/mapping."""
        endpoint = definition.endpoint_for(capability)
        if endpoint is None:
            raise ConnectorError("capability_not_executable")
        supplied = self._validated_arguments(endpoint, arguments)
        if endpoint.retry == "idempotent":
            if not idempotency_key:
                raise ConnectorError("idempotency_key_required")
            if supplied.get("idempotency_key") != idempotency_key:
                raise ConnectorError("idempotency_key_mismatch")

        base = self.base_url(definition, config)
        headers, auth_query, basic = self._authorize(definition, credential)
        mapped_query, body = self._mapped_request(endpoint, supplied)
        params: dict[str, Any] = {**auth_query, **mapped_query}
        if endpoint.idempotency_header:
            headers[endpoint.idempotency_header] = idempotency_key or ""
        headers["Accept"] = "application/json"
        if body:
            headers["Content-Type"] = "application/json"

        url = base + endpoint.path
        parsed = urlsplit(url)
        await self._host_validator(parsed.hostname or "", parsed.port or 443)

        try:
            response = await self._request(
                method=endpoint.method,
                url=url,
                headers=headers,
                params=params,
                body=body,
                basic=basic,
            )
        except httpx.TimeoutException:
            return ConnectorResult(ok=False, status_code=None, error_code="timeout")
        except httpx.HTTPError:
            return ConnectorResult(ok=False, status_code=None, error_code="transport_error")

        body_text = response.text[:MAX_RESPONSE_BYTES] if response.content else None
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
            return ConnectorResult(ok=True, status_code=response.status_code, body=body_text)
        code = {
            400: "provider_rejected",
            401: "unauthorized",
            403: "forbidden",
            404: "provider_not_found",
            409: "provider_conflict",
            422: "provider_rejected",
            429: "rate_limited",
        }.get(response.status_code, "provider_error")
        return ConnectorResult(ok=False, status_code=response.status_code, error_code=code)
