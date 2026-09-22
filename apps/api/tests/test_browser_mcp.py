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


def test_browser_adapter_sends_only_snapshotted_origin_and_relative_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
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

    settings = Settings(
        browser_runtime_url="http://browser.internal:8080",
        browser_runtime_token="x" * 32,
    )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = BrowserMcpAdapter(settings, client=client)
            return await adapter.call_tool_for_context(
                _context(),
                "page.inspect",
                {"path": "/services"},
                5.0,
            )
    result = asyncio.run(exercise())
    assert result["title"] == "Services"
    assert seen["authorization"] == "Bearer " + ("x" * 32)
    assert '"url":"https://example.com/services"' in seen["body"]
    assert '"allowed_origin":"https://example.com"' in seen["body"]


@pytest.mark.parametrize("path", ["//evil.example/path", "https://evil.example/", "relative"])
def test_browser_adapter_rejects_paths_that_can_escape_the_origin(path):
    settings = Settings(
        browser_runtime_url="http://browser.internal:8080",
        browser_runtime_token="x" * 32,
    )
    adapter = BrowserMcpAdapter(settings)

    async def exercise():
        await adapter.call_tool_for_context(_context(), "page.inspect", {"path": path}, 5.0)
    with pytest.raises(McpAdapterError) as raised:
        asyncio.run(exercise())
    assert raised.value.code == "browser_path_invalid"


def test_browser_adapter_requires_a_standard_run_snapshot():
    settings = Settings(
        browser_runtime_url="http://browser.internal:8080",
        browser_runtime_token="x" * 32,
    )
    adapter = BrowserMcpAdapter(settings)
    context = SimpleNamespace(agent_kind="custom", agent_snapshot=None)

    async def exercise():
        await adapter.call_tool_for_context(context, "page.inspect", {"path": "/"}, 5.0)
    with pytest.raises(McpAdapterError) as raised:
        asyncio.run(exercise())
    assert raised.value.code == "browser_standard_run_required"
