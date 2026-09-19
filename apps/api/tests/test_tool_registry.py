import pytest
from pydantic import BaseModel, ConfigDict

from nexora_api.tool_policy import default_policy
from nexora_api.tool_registry import (
    PolicyDecision,
    SideEffect,
    ToolGatewayError,
    ToolRegistry,
    ToolSpec,
)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class Output(BaseModel):
    value: str


async def handler(_context, arguments):
    return Output(value=arguments.value)


def spec(name="test.read", side_effect=SideEffect.READ, supports_idempotency=True):
    return ToolSpec(
        name=name,
        description="A narrow deterministic test tool.",
        input_model=Input,
        output_model=Output,
        side_effect=side_effect,
        handler=handler,
        supports_idempotency=supports_idempotency,
    )


def test_registry_contract_validation_and_defaults():
    registry = ToolRegistry()
    read = spec()
    registry.register(read)
    assert registry.get("test.read") is read
    assert default_policy(read).decision == PolicyDecision.ALLOW
    write = spec("test.write", SideEffect.WRITE)
    assert default_policy(write).decision == PolicyDecision.REQUIRE_APPROVAL

    with pytest.raises(ValueError):
        registry.register(read)
    with pytest.raises(ValueError):
        registry.register(spec("test.unsafe", SideEffect.WRITE, supports_idempotency=False))
    with pytest.raises(ToolGatewayError) as unknown:
        registry.get("missing.tool")
    assert unknown.value.code == "unknown_tool"


def test_registry_rejects_unknown_fields_and_large_arguments():
    registry = ToolRegistry()
    read = spec()
    registry.register(read)

    with pytest.raises(ToolGatewayError) as invalid:
        registry.validate_arguments(read, {"value": "ok", "extra": True})
    assert invalid.value.code == "invalid_tool_arguments"

    with pytest.raises(ToolGatewayError) as large:
        registry.validate_arguments(read, {"value": "x" * (33 * 1024)})
    assert large.value.code == "tool_arguments_too_large"
