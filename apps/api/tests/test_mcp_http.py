import asyncio
import json

import httpx
import pytest

from nexora_api.mcp_gateway import McpAdapterError
from nexora_api.mcp_http import McpHttpEndpoint, StreamableHttpMcpAdapter


def run_call(handler, *, endpoint=None):
    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = StreamableHttpMcpAdapter(
            endpoint or McpHttpEndpoint("https://mcp.example.test/mcp"),
            client=client,
        )
        try:
            return await adapter.call_tool("search", {"q": "otters"}, 10)
        finally:
            await client.aclose()

    return asyncio.run(exercise())


def test_modern_tool_call_uses_required_headers_and_returns_structured_content():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["headers"] = request.headers
        captured["body"] = body
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": "two results"}],
                    "structuredContent": {"count": 2},
                },
            },
        )

    result = run_call(handler)

    assert result == {"count": 2}
    assert captured["headers"]["mcp-protocol-version"] == "2026-07-28"
    assert captured["headers"]["mcp-method"] == "tools/call"
    assert captured["headers"]["mcp-name"] == "search"
    assert captured["headers"]["accept"] == "application/json, text/event-stream"
    assert captured["body"]["method"] == "tools/call"
    assert captured["body"]["params"]["name"] == "search"
    assert captured["body"]["params"]["arguments"] == {"q": "otters"}
    meta = captured["body"]["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "nexora-agentos"


def test_bearer_token_is_transport_only_and_text_result_is_bounded_object():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "resultType": "complete",
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "text", "text": "second"},
                    ],
                },
            },
        )

    result = run_call(
        handler,
        endpoint=McpHttpEndpoint(
            "https://mcp.example.test/mcp",
            bearer_token="secret-operator-token",
        ),
    )

    assert result == {"text": "first\nsecond"}
    assert captured["authorization"] == "Bearer secret-operator-token"


def test_event_stream_response_selects_the_matching_jsonrpc_result():
    def handler(request: httpx.Request) -> httpx.Response:
        request_id = json.loads(request.content)["id"]
        stream = (
            'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
            + "data: "
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "resultType": "complete",
                        "content": [{"type": "text", "text": "ok"}],
                        "structuredContent": {"ok": True},
                    },
                }
            )
            + "\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=stream,
        )

    assert run_call(handler) == {"ok": True}


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (302, "mcp_redirect_forbidden", False),
        (401, "mcp_auth_error", False),
        (404, "mcp_server_not_found", False),
        (429, "mcp_unavailable", True),
        (503, "mcp_unavailable", True),
        (422, "mcp_bad_request", False),
    ],
)
def test_http_failures_are_normalized_without_upstream_body(status, code, retryable):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="sensitive upstream detail")

    with pytest.raises(McpAdapterError) as error:
        run_call(handler)

    assert error.value.code == code
    assert error.value.retryable is retryable
    assert "sensitive upstream detail" not in str(error.value)


@pytest.mark.parametrize(
    "result_type,code",
    [
        ("input_required", "mcp_input_required_unsupported"),
        ("task", "mcp_result_type_unsupported"),
        (None, "mcp_result_type_unsupported"),
    ],
)
def test_non_complete_modern_results_fail_closed(result_type, code):
    def handler(request: httpx.Request) -> httpx.Response:
        request_id = json.loads(request.content)["id"]
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": result_type,
                    "content": [],
                },
            },
        )

    with pytest.raises(McpAdapterError, match=code):
        run_call(handler)


def test_tool_reported_error_is_not_persisted_as_success():
    def handler(request: httpx.Request) -> httpx.Response:
        request_id = json.loads(request.content)["id"]
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": "private server error"}],
                    "isError": True,
                },
            },
        )

    with pytest.raises(McpAdapterError) as error:
        run_call(handler)

    assert error.value.code == "mcp_tool_error"
    assert "private server error" not in str(error.value)


def test_response_size_limit_and_non_text_fallback_fail_closed():
    def oversized(request: httpx.Request) -> httpx.Response:
        request_id = json.loads(request.content)["id"]
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "content": [{"type": "text", "text": "x" * 4000}],
                },
            },
        )

    endpoint = McpHttpEndpoint(
        "https://mcp.example.test/mcp",
        max_response_bytes=1024,
    )
    with pytest.raises(McpAdapterError, match="mcp_response_too_large"):
        run_call(oversized, endpoint=endpoint)

    def image_only(request: httpx.Request) -> httpx.Response:
        request_id = json.loads(request.content)["id"]
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resultType": "complete",
                    "content": [{"type": "image", "data": "base64", "mimeType": "image/png"}],
                },
            },
        )

    with pytest.raises(McpAdapterError, match="mcp_unstructured_result_unsupported"):
        run_call(image_only)


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.test/mcp",
        "https://localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://10.0.0.1/mcp",
        "https://user:secret@mcp.example.test/mcp",
        "https://mcp.example.test:8443/mcp",
        "https://mcp.example.test/mcp?token=secret",
    ],
)
def test_endpoint_rejects_unsafe_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        McpHttpEndpoint(url)
