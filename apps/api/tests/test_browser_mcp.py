import asyncio
from types import SimpleNamespace

import httpx
import pytest

from nexora_api.browser_mcp import BrowserMcpAdapter
from nexora_api.config import Settings
from nexora_api.mcp_gateway import McpAdapterError


def _context(origin="https://example.com"):
    return SimpleNamespace(
        agent_kind="standard",
        agent_snapshot={"browser": {"allowed_origin": origin}},
    )


def _settings():
    return Settings(
        browser_runtime_url="http://browser.internal:8080",
        browser_runtime_token="x" * 32,
    )


def test_browser_adapter_sends_only_snapshotted_origin_and_relative_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers["Authorization"]
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "ok": True,
                "url": "https://example.com/services",
                "status_code": 200,
                "title": "Services",
                "text": "Rendered page",
                "links": [],
                "truncated": False,
            },
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = BrowserMcpAdapter(_settings(), client=client)
            return await adapter.call_tool_for_context(
                _context(),
                "page.inspect",
                {"path": "/services"},
                5.0,
            )

    result = asyncio.run(exercise())
    assert result["title"] == "Services"
    assert seen["path"] == "/inspect"
    assert seen["authorization"] == "Bearer " + ("x" * 32)
    assert '"url":"https://example.com/services"' in seen["body"]
    assert '"allowed_origin":"https://example.com"' in seen["body"]


def test_browser_action_is_bounded_and_does_not_forward_idempotency_metadata():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "ok": True,
                "url": "https://example.com/contact",
                "status_code": 200,
                "title": "Contact",
                "text": "Saved",
                "links": [],
                "truncated": False,
                "action": "fill",
            },
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = BrowserMcpAdapter(_settings(), client=client)
            return await adapter.call_tool_for_context(
                _context(),
                "page.action",
                {
                    "path": "/contact",
                    "action": "fill",
                    "selector": "#message",
                    "value": "Hello",
                    "idempotency_key": "browser-action-0001",
                },
                5.0,
            )

    result = asyncio.run(exercise())
    assert result["action"] == "fill"
    assert seen["path"] == "/action"
    assert '"selector":"#message"' in seen["body"]
    assert '"value":"Hello"' in seen["body"]
    assert "idempotency_key" not in seen["body"]


@pytest.mark.parametrize("path", ["//evil.example/path", "https://evil.example/", "relative"])
def test_browser_adapter_rejects_paths_that_can_escape_the_origin(path):
    adapter = BrowserMcpAdapter(_settings())

    async def exercise():
        await adapter.call_tool_for_context(_context(), "page.inspect", {"path": path}, 5.0)

    with pytest.raises(McpAdapterError) as raised:
        asyncio.run(exercise())
    assert raised.value.code == "browser_path_invalid"


def test_browser_action_network_failure_is_not_retried_after_possible_mutation():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = BrowserMcpAdapter(_settings(), client=client)
            await adapter.call_tool_for_context(
                _context(),
                "page.action",
                {"path": "/", "action": "click", "selector": "#save"},
                5.0,
            )

    with pytest.raises(McpAdapterError) as raised:
        asyncio.run(exercise())
    assert raised.value.code == "browser_runtime_unavailable"
    assert raised.value.retryable is False


def test_browser_adapter_requires_a_standard_run_snapshot():
    adapter = BrowserMcpAdapter(_settings())
    context = SimpleNamespace(agent_kind="custom", agent_snapshot=None)

    async def exercise():
        await adapter.call_tool_for_context(context, "page.inspect", {"path": "/"}, 5.0)

    with pytest.raises(McpAdapterError) as raised:
        asyncio.run(exercise())
    assert raised.value.code == "browser_standard_run_required"
