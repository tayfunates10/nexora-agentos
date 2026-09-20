from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from nexora_api.model_routing import (
    ModelCapability,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderTool,
)
from nexora_api.openai_responses import OpenAIResponsesAdapter

CAPABILITIES = frozenset(
    {
        ModelCapability.TEXT,
        ModelCapability.TOOLS,
        ModelCapability.STRUCTURED_OUTPUT,
        ModelCapability.STREAMING,
    }
)


def request(*, request_id: str = "req-1") -> ProviderRequest:
    return ProviderRequest(
        request_id=request_id,
        messages=(ProviderMessage(role="user", content="Create a ticket"),),
        tools=(
            ProviderTool(
                name="ticket.create",
                description="Create a ticket",
                input_schema={
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                    "additionalProperties": False,
                },
            ),
        ),
        response_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
        max_output_tokens=256,
    )


def adapter(client: httpx.AsyncClient) -> OpenAIResponsesAdapter:
    return OpenAIResponsesAdapter(
        api_key=SecretStr("test-secret-key"),
        model_capabilities={"gpt-test": CAPABILITIES},
        client=client,
    )


def test_generate_serializes_contract_and_normalizes_response():
    captured: dict = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(http_request.headers)
        captured["body"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"summary":"queued"}',
                            }
                        ],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "ticket.create",
                        "arguments": '{"title":"Help"}',
                    },
                ],
                "usage": {"input_tokens": 11, "output_tokens": 7},
            },
        )

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await adapter(client).generate(
                model="gpt-test",
                request=request(),
                timeout_seconds=2,
            )
        finally:
            await client.aclose()
        return result

    result = asyncio.run(exercise())

    assert captured["headers"]["authorization"] == "Bearer test-secret-key"
    assert captured["body"]["store"] is False
    assert captured["body"]["tools"][0]["strict"] is True
    assert captured["body"]["text"]["format"]["type"] == "json_schema"
    assert captured["body"]["text"]["format"]["strict"] is True
    assert captured["body"]["max_output_tokens"] == 256
    assert result.structured_output == {"summary": "queued"}
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].arguments == {"title": "Help"}
    assert result.usage.input_tokens == 11
    assert result.finish_reason == "tool_call"


def test_rate_limit_is_retryable_and_does_not_leak_provider_body():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="sensitive-upstream-body")

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ProviderError) as error:
                await adapter(client).generate(
                    model="gpt-test",
                    request=request(),
                    timeout_seconds=2,
                )
        finally:
            await client.aclose()
        return error.value

    error = asyncio.run(exercise())

    assert error.code == "provider_rate_limited"
    assert error.retryable is True
    assert str(error) == "provider_rate_limited"
    assert "sensitive-upstream-body" not in str(error)


def test_unknown_model_fails_closed_before_network_call():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        instance = adapter(client)
        try:
            with pytest.raises(ProviderError) as error:
                await instance.generate(
                    model="unknown",
                    request=request(),
                    timeout_seconds=2,
                )
        finally:
            await client.aclose()
        return error.value

    error = asyncio.run(exercise())

    assert error.code == "provider_model_not_configured"
    assert calls == 0


def test_cancel_interrupts_active_provider_request():
    async def exercise():
        started = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        instance = adapter(client)
        task = asyncio.create_task(
            instance.generate(
                model="gpt-test",
                request=request(request_id="cancel-me"),
                timeout_seconds=30,
            )
        )
        await started.wait()
        await instance.cancel("cancel-me")
        try:
            with pytest.raises(ProviderError) as error:
                await task
        finally:
            await client.aclose()
        return error.value

    error = asyncio.run(exercise())

    assert error.code == "provider_cancelled"
    assert error.retryable is False


def test_stream_normalizes_text_tool_usage_and_completion():
    events = [
        {"type": "response.output_text.delta", "delta": "Hello"},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call-2",
                "name": "ticket.create",
                "arguments": '{"title":"Stream"}',
            },
        },
        {
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 5, "output_tokens": 3}},
        },
    ]
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body.encode(),
            headers={"content-type": "text/event-stream"},
        )

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return [
                event
                async for event in adapter(client).stream(
                    model="gpt-test",
                    request=request(request_id="stream-1"),
                    timeout_seconds=2,
                )
            ]
        finally:
            await client.aclose()

    normalized = asyncio.run(exercise())

    assert [event.type for event in normalized] == [
        "text_delta",
        "tool_call",
        "usage",
        "completed",
    ]
    assert normalized[0].text_delta == "Hello"
    assert normalized[1].tool_call is not None
    assert normalized[1].tool_call.arguments == {"title": "Stream"}
    assert normalized[2].usage is not None
    assert normalized[2].usage.output_tokens == 3


def test_tool_history_serialization_preserves_call_identity():
    from nexora_api.model_routing import ProviderToolCall

    call = ProviderToolCall("call-42", "lookup", {"query": "question"})
    encoded = OpenAIResponsesAdapter._message_payload(
        ProviderMessage("assistant", "", tool_call=call)
    )
    assert encoded["type"] == "function_call"
    assert encoded["call_id"] == "call-42"
    assert json.loads(encoded["arguments"]) == {"query": "question"}
    result = OpenAIResponsesAdapter._message_payload(
        ProviderMessage("tool", '{"answer":42}', tool_call_id="call-42")
    )
    assert result == {
        "type": "function_call_output",
        "call_id": "call-42",
        "output": '{"answer":42}',
    }
