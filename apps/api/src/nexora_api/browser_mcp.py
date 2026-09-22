"""Context-aware read-only browser tool for standard agents."""

from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from nexora_api.config import Settings
from nexora_api.mcp_gateway import McpAdapterError

BROWSER_SERVER_KEY = "browser"


class BrowserMcpAdapter:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        if settings.browser_runtime_url is None or settings.browser_runtime_token is None:
            raise ValueError("browser runtime is not configured")
        self.base_url = settings.browser_runtime_url
        self.token = settings.browser_runtime_token.get_secret_value()
        self.client = client

    @staticmethod
    def _origin(snapshot: dict[str, Any] | None) -> str:
        if not isinstance(snapshot, dict):
            raise McpAdapterError("browser_snapshot_missing", retryable=False)
        browser = snapshot.get("browser")
        if not isinstance(browser, dict):
            raise McpAdapterError("browser_snapshot_missing", retryable=False)
        origin = browser.get("allowed_origin")
        if not isinstance(origin, str):
            raise McpAdapterError("browser_snapshot_invalid", retryable=False)
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise McpAdapterError("browser_snapshot_invalid", retryable=False)
        return origin.rstrip("/")

    async def call_tool_for_context(
        self,
        context,
        remote_name: str,
        arguments: dict[str, object],
        timeout_seconds: float,
    ) -> Any:
        if context.agent_kind != "standard" or remote_name != "page.inspect":
            raise McpAdapterError("browser_standard_run_required", retryable=False)
        origin = self._origin(context.agent_snapshot)
        path = arguments.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise McpAdapterError("browser_path_invalid", retryable=False)
        target = urljoin(origin + "/", path.lstrip("/"))
        if urlsplit(target).netloc != urlsplit(origin).netloc:
            raise McpAdapterError("browser_origin_not_allowed", retryable=False)

        headers = {"Authorization": "Bearer " + self.token}
        payload = {"url": target, "allowed_origin": origin}
        try:
            if self.client is not None:
                response = await self.client.post(
                    self.base_url + "/inspect",
                    json=payload,
                    headers=headers,
                    timeout=timeout_seconds,
                )
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        self.base_url + "/inspect",
                        json=payload,
                        headers=headers,
                        timeout=timeout_seconds,
                    )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise McpAdapterError("browser_runtime_unavailable", retryable=True) from exc

        if response.status_code == 403:
            raise McpAdapterError("browser_origin_not_allowed", retryable=False)
        if response.status_code >= 500:
            raise McpAdapterError("browser_runtime_error", retryable=True)
        if not response.is_success:
            raise McpAdapterError("browser_request_rejected", retryable=False)
        try:
            result = response.json()
        except ValueError as exc:
            raise McpAdapterError("browser_invalid_response", retryable=True) from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise McpAdapterError("browser_invalid_response", retryable=True)
        return result
