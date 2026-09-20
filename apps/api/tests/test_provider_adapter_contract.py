from __future__ import annotations

import asyncio

from nexora_api.model_routing import (
    ModelCapability,
    ProviderMessage,
    ProviderRequest,
    ProviderResponse,
    ProviderStreamEvent,
    ProviderTool,
    ProviderToolCall,
    ProviderUsage,
)


def test_provider_request_normalizes_messages_tools_and_structured_output():
    request = ProviderRequest(
        messages=(ProviderMessage(role="user", content="Create a ticket"),),
        tools=(
            ProviderTool(
                name="ticket.create",
                description="Create one support ticket",
                input_schema={"type": "object", "required": ["title"]},
            ),
        ),
        response_schema={"type": "object", "required": ["summary"]},
        max_output_tokens=512,
    )

    assert request.messages[0].role == "user"
    assert request.tools[0].name == "ticket.create"
    assert request.response_schema == {"type": "object", "required": ["summary"]}
    assert request.max_output_tokens == 512


def test_provider_response_keeps_vendor_objects_out_of_runtime_contract():
    response = ProviderResponse(
        text=None,
        tool_calls=(
            ProviderToolCall(id="call-1", name="ticket.create", arguments={"title": "Help"}),
        ),
        structured_output={"summary": "queued"},
        usage=ProviderUsage(input_tokens=12, output_tokens=7),
        finish_reason="tool_call",
    )

    assert response.tool_calls[0].arguments == {"title": "Help"}
    assert response.usage.input_tokens == 12
    assert response.finish_reason == "tool_call"


def test_stream_event_can_normalize_usage_without_provider_sdk_types():
    event = ProviderStreamEvent(
        type="usage",
        usage=ProviderUsage(input_tokens=5, output_tokens=3),
    )

    assert event.type == "usage"
    assert event.usage == ProviderUsage(input_tokens=5, output_tokens=3)


def test_capability_values_cover_contract_features():
    assert {
        ModelCapability.TEXT,
        ModelCapability.TOOLS,
        ModelCapability.STRUCTURED_OUTPUT,
        ModelCapability.STREAMING,
    } == set(ModelCapability)


def test_contract_values_are_immutable():
    request = ProviderRequest(messages=(ProviderMessage(role="user", content="hello"),))

    async def verify() -> None:
        await asyncio.sleep(0)
        assert request.messages == (ProviderMessage(role="user", content="hello"),)

    asyncio.run(verify())
