import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

MAX_TOOL_ARGUMENT_BYTES = 32 * 1024
MAX_TOOL_RESULT_BYTES = 64 * 1024


class SideEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXTERNAL_COMMUNICATION = "external_communication"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class ToolGatewayError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ToolExecutionError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ToolApprovalRequired(Exception):
    def __init__(self, tool_call_id: UUID, approval_id: UUID):
        super().__init__("tool_approval_required")
        self.tool_call_id = tool_call_id
        self.approval_id = approval_id


@dataclass(frozen=True)
class ToolExecutionContext:
    tool_call_id: UUID
    workspace_id: UUID
    run_id: UUID
    actor_issuer: str
    actor_subject: str
    idempotency_key: str


ToolHandler = Callable[[ToolExecutionContext, BaseModel], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    side_effect: SideEffect
    handler: ToolHandler
    schema_version: int = 1
    supports_idempotency: bool = True

    def contract(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "schema_version": self.schema_version,
            "side_effect": self.side_effect,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec):
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,99}", spec.name):
            raise ValueError("invalid tool name")
        if not 1 <= len(spec.description) <= 500:
            raise ValueError("invalid tool description")
        if spec.schema_version < 1:
            raise ValueError("invalid schema version")
        if spec.side_effect != SideEffect.READ and not spec.supports_idempotency:
            raise ValueError("mutation tools must support idempotency")
        if spec.name in self._tools:
            raise ValueError("duplicate tool")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolGatewayError("unknown_tool") from exc

    def list(self) -> list[ToolSpec]:
        return [self._tools[name] for name in sorted(self._tools)]

    def validate_arguments(self, spec: ToolSpec, arguments: dict[str, Any]) -> BaseModel:
        try:
            encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
        except (TypeError, ValueError) as exc:
            raise ToolGatewayError("invalid_tool_arguments") from exc
        if len(encoded) > MAX_TOOL_ARGUMENT_BYTES:
            raise ToolGatewayError("tool_arguments_too_large")
        try:
            return spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolGatewayError("invalid_tool_arguments") from exc

    async def execute(
        self, spec: ToolSpec, context: ToolExecutionContext, arguments: BaseModel
    ) -> BaseModel:
        result = await spec.handler(context, arguments)
        try:
            output = spec.output_model.model_validate(result)
        except ValidationError as exc:
            raise ToolExecutionError("invalid_tool_result") from exc
        encoded = json.dumps(
            output.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        if len(encoded) > MAX_TOOL_RESULT_BYTES:
            raise ToolExecutionError("tool_result_too_large")
        return output
