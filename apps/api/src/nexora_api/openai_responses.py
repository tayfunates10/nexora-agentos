from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from nexora_api.model_routing import (
    ModelCapability,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderStreamEvent,
    ProviderToolCall,
    ProviderUsage,
)

_RESPONSES_URL = "https://api.openai.com/v1/responses"


class OpenAIResponsesAdapter:
    """OpenAI Responses API adapter with normalized Nexora contracts."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model_capabilities: Mapping[str, frozenset[ModelCapability]],
        client: httpx.AsyncClient | None = None,
    ):
        if not api_key.get_secret_value():
            raise ValueError("api_key must not be empty")
        if not model_capabilities:
            raise ValueError("model_capabilities must not be empty")
        self._api_key = api_key
        self._model_capabilities = dict(model_capabilities)
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}

    @property
    def name(self) -> str:
        return "openai"

    def capabilities(self, model: str) -> frozenset[ModelCapability]:
        capabilities = self._model_capabilities.get(model)
        if capabilities is None:
            raise ProviderError("provider_model_not_configured")
        return capabilities

    async def generate(
        self,
        *,
        model: str,
        request: ProviderRequest,
        timeout_seconds: float,
    ) -> ProviderResponse:
        self._require_capabilities(model, request, streaming=False)
        task = self._register_request(request.request_id)
        try:
            response = await self._client.post(
                _RESPONSES_URL,
                json=self._payload(model, request),
                headers=self._headers(),
                timeout=timeout_seconds,
            )
            self._raise_for_status(response)
            return self._parse_response(
                self._decode_json(response),
                structured=request.response_schema is not None,
            )
        except asyncio.CancelledError as exc:
            raise ProviderError("provider_cancelled") from exc
        except httpx.TimeoutException as exc:
            raise ProviderError("provider_timeout", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderError("provider_unavailable", retryable=True) from exc
        finally:
            self._unregister_request(request.request_id, task)

    async def stream(
        self,
        *,
        model: str,
        request: ProviderRequest,
        timeout_seconds: float,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self._require_capabilities(model, request, streaming=True)
        task = self._register_request(request.request_id)
        try:
            payload = self._payload(model, request)
            payload["stream"] = True
            async with self._client.stream(
                "POST",
                _RESPONSES_URL,
                json=payload,
                headers=self._headers(),
                timeout=timeout_seconds,
            ) as response:
                self._raise_for_status(response)
                async for line in response.aiter_lines():
                    event = self._decode_sse_line(line)
                    if event is None:
                        continue
                    for normalized in self._normalize_stream_event(event):
                        yield normalized
        except asyncio.CancelledError as exc:
            raise ProviderError("provider_cancelled") from exc
        except httpx.TimeoutException as exc:
            raise ProviderError("provider_timeout", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderError("provider_unavailable", retryable=True) from exc
        finally:
            self._unregister_request(request.request_id, task)

    async def cancel(self, request_id: str) -> None:
        task = self._active_tasks.get(request_id)
        if task is not None and not task.done():
            task.cancel()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self._api_key.get_secret_value(),
            "Content-Type": "application/json",
        }

    def _payload(self, model: str, request: ProviderRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "store": False,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "strict": True,
                }
                for tool in request.tools
            ]
        if request.response_schema is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "nexora_response",
                    "strict": True,
                    "schema": request.response_schema,
                }
            }
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        return payload

    def _require_capabilities(
        self,
        model: str,
        request: ProviderRequest,
        *,
        streaming: bool,
    ) -> None:
        configured = self.capabilities(model)
        required = {ModelCapability.TEXT}
        if request.tools:
            required.add(ModelCapability.TOOLS)
        if request.response_schema is not None:
            required.add(ModelCapability.STRUCTURED_OUTPUT)
        if streaming:
            required.add(ModelCapability.STREAMING)
        if not required.issubset(configured):
            raise ProviderError("provider_capability_mismatch")

    def _register_request(self, request_id: str) -> asyncio.Task[Any]:
        if not request_id or not request_id.strip():
            raise ProviderError("provider_request_id_required")
        current = asyncio.current_task()
        if current is None:
            raise ProviderError("provider_runtime_unavailable", retryable=True)
        existing = self._active_tasks.get(request_id)
        if existing is not None and not existing.done():
            raise ProviderError("duplicate_provider_request")
        self._active_tasks[request_id] = current
        return current

    def _unregister_request(self, request_id: str, task: asyncio.Task[Any]) -> None:
        if self._active_tasks.get(request_id) is task:
            self._active_tasks.pop(request_id, None)

    @staticmethod
    def _decode_json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("provider_invalid_response") from exc
        if not isinstance(data, dict):
            raise ProviderError("provider_invalid_response")
        return data

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status == 429:
            raise ProviderError("provider_rate_limited", retryable=True)
        if status in {408, 409} or 500 <= status < 600:
            raise ProviderError("provider_unavailable", retryable=True)
        if status in {401, 403}:
            raise ProviderError("provider_auth_error")
        if status in {400, 404, 422}:
            raise ProviderError("provider_bad_request")
        raise ProviderError("provider_http_error")

    def _parse_response(
        self,
        data: dict[str, Any],
        *,
        structured: bool,
    ) -> ProviderResponse:
        status = data.get("status")
        if status == "failed":
            raise ProviderError("provider_failed")
        output = data.get("output")
        usage = data.get("usage")
        if not isinstance(output, list) or not isinstance(usage, dict):
            raise ProviderError("provider_invalid_response")

        text_parts: list[str] = []
        tool_calls: list[ProviderToolCall] = []
        refusal = False
        for item in output:
            if not isinstance(item, dict):
                raise ProviderError("provider_invalid_response")
            item_type = item.get("type")
            if item_type == "message":
                content = item.get("content", [])
                if not isinstance(content, list):
                    raise ProviderError("provider_invalid_response")
                for part in content:
                    if not isinstance(part, dict):
                        raise ProviderError("provider_invalid_response")
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
                    elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                        text_parts.append(part["refusal"])
                        refusal = True
            elif item_type == "function_call":
                tool_calls.append(self._parse_tool_call(item))

        text = "".join(text_parts) or None
        structured_output = None
        if text is not None and structured:
            structured_output = self._parse_structured_text(text)

        if refusal:
            finish_reason = "refusal"
        elif tool_calls:
            finish_reason = "tool_call"
        elif status == "incomplete":
            finish_reason = "incomplete"
        elif status == "completed":
            finish_reason = "stop"
        else:
            finish_reason = str(status or "unknown")

        return ProviderResponse(
            text=text,
            tool_calls=tuple(tool_calls),
            structured_output=structured_output,
            usage=self._parse_usage(usage),
            finish_reason=finish_reason,
        )

    @staticmethod
    def _parse_structured_text(text: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("provider_invalid_structured_output") from exc
        if not isinstance(value, dict):
            raise ProviderError("provider_invalid_structured_output")
        return value

    @staticmethod
    def _parse_tool_call(item: dict[str, Any]) -> ProviderToolCall:
        call_id = item.get("call_id")
        name = item.get("name")
        arguments = item.get("arguments")
        if (
            not isinstance(call_id, str)
            or not isinstance(name, str)
            or not isinstance(arguments, str)
        ):
            raise ProviderError("provider_invalid_response")
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ProviderError("provider_invalid_tool_arguments") from exc
        if not isinstance(parsed, dict):
            raise ProviderError("provider_invalid_tool_arguments")
        return ProviderToolCall(id=call_id, name=name, arguments=parsed)

    @staticmethod
    def _parse_usage(usage: dict[str, Any]) -> ProviderUsage:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            raise ProviderError("provider_invalid_response")
        return ProviderUsage(input_tokens=input_tokens, output_tokens=output_tokens)

    @staticmethod
    def _decode_sse_line(line: str) -> dict[str, Any] | None:
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProviderError("provider_invalid_stream") from exc
        if not isinstance(event, dict):
            raise ProviderError("provider_invalid_stream")
        return event

    def _normalize_stream_event(
        self, event: dict[str, Any]
    ) -> tuple[ProviderStreamEvent, ...]:
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ProviderError("provider_invalid_stream")
            return (ProviderStreamEvent(type="text_delta", text_delta=delta),)

        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                return (
                    ProviderStreamEvent(
                        type="tool_call",
                        tool_call=self._parse_tool_call(item),
                    ),
                )
            return ()

        if event_type == "response.completed":
            response = event.get("response")
            if not isinstance(response, dict):
                raise ProviderError("provider_invalid_stream")
            usage = response.get("usage")
            if not isinstance(usage, dict):
                raise ProviderError("provider_invalid_stream")
            return (
                ProviderStreamEvent(type="usage", usage=self._parse_usage(usage)),
                ProviderStreamEvent(type="completed"),
            )

        if event_type == "response.incomplete":
            return (ProviderStreamEvent(type="incomplete"),)

        if event_type in {"response.failed", "error"}:
            raise ProviderError("provider_stream_error")

        return ()
