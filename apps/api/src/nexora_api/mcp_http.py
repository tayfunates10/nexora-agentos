from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from nexora_api.mcp_gateway import McpAdapterError

MCP_PROTOCOL_VERSION = "2026-07-28"
CLIENT_INFO = {"name": "nexora-agentos", "version": "0.1.0"}
DEFAULT_MAX_RESPONSE_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class McpHttpEndpoint:
    url: str
    bearer_token: str | None = None
    timeout_seconds: float = 15.0
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise ValueError("MCP endpoint must be an HTTPS URL on port 443 without credentials/query")
        if not 0.1 <= self.timeout_seconds <= 120:
            raise ValueError("MCP endpoint timeout must be between 0.1 and 120 seconds")
        if not 1024 <= self.max_response_bytes <= 1024 * 1024:
            raise ValueError("MCP response limit must be between 1 KiB and 1 MiB")


class StreamableHttpMcpAdapter:
    """Constrained MCP 2026-07-28 client for operator-allowlisted tool servers.

    The adapter intentionally supports one operation: stateless tools/call. It never
    follows redirects, discovers arbitrary endpoints, executes stdio commands, or exposes
    credentials to the model. Multi-round-trip and Tasks extension results fail closed.
    """

    def __init__(
        self,
        endpoint: McpHttpEndpoint,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None

    async def call_tool(
        self,
        remote_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> Any:
        if not remote_name or len(remote_name) > 128:
            raise McpAdapterError("mcp_invalid_tool_name", retryable=False)

        request_id = "nexora-" + uuid4().hex
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": remote_name,
                "arguments": arguments,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": remote_name,
        }
        if self.endpoint.bearer_token:
            headers["Authorization"] = "Bearer " + self.endpoint.bearer_token

        timeout = min(timeout_seconds, self.endpoint.timeout_seconds)
        try:
            async with self._client.stream(
                "POST",
                self.endpoint.url,
                headers=headers,
                json=body,
                timeout=timeout,
            ) as response:
                self._raise_for_status(response)
                payload = await self._read_bounded(response)
                message = self._decode_response(
                    payload,
                    response.headers.get("content-type", ""),
                    request_id,
                )
        except httpx.TimeoutException as exc:
            raise TimeoutError("mcp_timeout") from exc
        except httpx.TransportError as exc:
            raise McpAdapterError("mcp_unavailable", retryable=True) from exc

        return self._tool_result(message)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if 300 <= status < 400:
            raise McpAdapterError("mcp_redirect_forbidden", retryable=False)
        if status in {401, 403}:
            raise McpAdapterError("mcp_auth_error", retryable=False)
        if status == 404:
            raise McpAdapterError("mcp_server_not_found", retryable=False)
        if status in {408, 409, 425, 429} or 500 <= status < 600:
            raise McpAdapterError("mcp_unavailable", retryable=True)
        raise McpAdapterError("mcp_bad_request", retryable=False)

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self.endpoint.max_response_bytes:
                raise McpAdapterError("mcp_response_too_large", retryable=False)
            chunks.append(chunk)
        return b"".join(chunks)

    @classmethod
    def _decode_response(
        cls,
        payload: bytes,
        content_type: str,
        request_id: str,
    ) -> dict[str, Any]:
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "application/json":
            message = cls._decode_json(payload)
            if not isinstance(message, dict):
                raise McpAdapterError("mcp_invalid_response", retryable=False)
            cls._validate_response_id(message, request_id)
            return message
        if media_type == "text/event-stream":
            for message in cls._decode_sse(payload):
                if isinstance(message, dict) and message.get("id") == request_id:
                    cls._validate_response_id(message, request_id)
                    return message
            raise McpAdapterError("mcp_response_missing", retryable=False)
        raise McpAdapterError("mcp_unsupported_content_type", retryable=False)

    @staticmethod
    def _decode_json(payload: bytes) -> Any:
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpAdapterError("mcp_invalid_response", retryable=False) from exc

    @classmethod
    def _decode_sse(cls, payload: bytes) -> tuple[Any, ...]:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise McpAdapterError("mcp_invalid_response", retryable=False) from exc

        messages: list[Any] = []
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        for event in normalized.split("\n\n"):
            data_lines = []
            for line in event.split("\n"):
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            raw = "\n".join(data_lines)
            try:
                messages.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise McpAdapterError("mcp_invalid_response", retryable=False) from exc
        return tuple(messages)

    @staticmethod
    def _validate_response_id(message: dict[str, Any], request_id: str) -> None:
        if message.get("jsonrpc") != "2.0" or message.get("id") != request_id:
            raise McpAdapterError("mcp_invalid_response", retryable=False)

    @staticmethod
    def _tool_result(message: dict[str, Any]) -> Any:
        error = message.get("error")
        if error is not None:
            if not isinstance(error, dict) or not isinstance(error.get("code"), int):
                raise McpAdapterError("mcp_invalid_response", retryable=False)
            raise McpAdapterError("mcp_protocol_error", retryable=False)

        result = message.get("result")
        if not isinstance(result, dict):
            raise McpAdapterError("mcp_invalid_response", retryable=False)
        result_type = result.get("resultType")
        if result_type == "input_required":
            raise McpAdapterError("mcp_input_required_unsupported", retryable=False)
        if result_type != "complete":
            raise McpAdapterError("mcp_result_type_unsupported", retryable=False)
        if result.get("isError") is True:
            raise McpAdapterError("mcp_tool_error", retryable=False)

        if "structuredContent" in result:
            return result["structuredContent"]

        content = result.get("content")
        if not isinstance(content, list):
            raise McpAdapterError("mcp_invalid_response", retryable=False)
        text_parts: list[str] = []
        for block in content:
            if (
                not isinstance(block, dict)
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
            ):
                raise McpAdapterError("mcp_unstructured_result_unsupported", retryable=False)
            text_parts.append(block["text"])
        return {"text": "\n".join(text_parts)}
