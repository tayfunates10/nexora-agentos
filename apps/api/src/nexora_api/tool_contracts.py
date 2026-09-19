import hashlib
import json
from dataclasses import dataclass
from typing import Any

MAX_ARGUMENT_BYTES = 32 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_SCHEMA_BYTES = 32 * 1024
MAX_CONTRACT_BYTES = 96 * 1024

_INPUT_SCHEMA_KEYS = {
    "$schema",
    "title",
    "description",
    "type",
    "properties",
    "required",
    "additionalProperties",
}
_VALUE_SCHEMA_KEYS = {
    "title",
    "description",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
}
_JSON_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


@dataclass(frozen=True)
class ToolContractError(ValueError):
    code: str

    def __str__(self) -> str:
        return self.code


def canonical_json(value: Any, max_bytes: int) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ToolContractError("invalid_json") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ToolContractError("payload_too_large")
    return encoded


def hash_arguments(arguments: dict[str, Any]) -> tuple[str, str]:
    canonical = canonical_json(arguments, MAX_ARGUMENT_BYTES)
    return canonical, hashlib.sha256(canonical.encode()).hexdigest()


def hash_tool_contract(
    *,
    server_key: str,
    remote_name: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None,
    side_effect: str,
) -> str:
    canonical = canonical_json(
        {
            "server_key": server_key,
            "remote_name": remote_name,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "side_effect": side_effect,
        },
        MAX_CONTRACT_BYTES,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_registration_schema(
    schema: dict[str, Any],
    *,
    side_effect: str | None = None,
    output: bool = False,
) -> None:
    canonical_json(schema, MAX_SCHEMA_BYTES)
    if schema.get("type") != "object":
        raise ToolContractError("schema_root_must_be_object")
    allowed = _INPUT_SCHEMA_KEYS if not output else _VALUE_SCHEMA_KEYS
    unknown = set(schema) - allowed
    if unknown:
        raise ToolContractError("unsupported_schema_keyword")
    _validate_schema_node(schema, root=True)

    if not output and schema.get("additionalProperties") is not False:
        raise ToolContractError("input_schema_must_forbid_unknown_fields")

    if not output and side_effect and side_effect != "read":
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        idempotency = properties.get("idempotency_key")
        if (
            not isinstance(idempotency, dict)
            or idempotency.get("type") != "string"
            or "idempotency_key" not in required
        ):
            raise ToolContractError("mutation_requires_idempotency_key")


def _validate_schema_node(schema: dict[str, Any], *, root: bool = False) -> None:
    if not isinstance(schema, dict):
        raise ToolContractError("invalid_schema")
    allowed = _INPUT_SCHEMA_KEYS if root else _VALUE_SCHEMA_KEYS
    if set(schema) - allowed:
        raise ToolContractError("unsupported_schema_keyword")

    value_type = schema.get("type")
    if value_type not in _JSON_TYPES:
        raise ToolContractError("unsupported_schema_type")

    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ToolContractError("invalid_schema_enum")

    if value_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ToolContractError("invalid_schema_properties")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise ToolContractError("invalid_schema_required")
        if len(required) != len(set(required)) or not set(required).issubset(properties):
            raise ToolContractError("invalid_schema_required")
        additional = schema.get("additionalProperties", False)
        if not isinstance(additional, bool):
            raise ToolContractError("unsupported_additional_properties")
        for child in properties.values():
            _validate_schema_node(child)
    elif value_type == "array":
        items = schema.get("items")
        if not isinstance(items, dict):
            raise ToolContractError("array_items_required")
        _validate_schema_node(items)


def validate_value(value: Any, schema: dict[str, Any]) -> None:
    value_type = schema["type"]
    if value_type == "object":
        if not isinstance(value, dict):
            raise ToolContractError("schema_type_mismatch")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if any(key not in value for key in required):
            raise ToolContractError("schema_required_field_missing")
        if schema.get("additionalProperties", False) is False and set(value) - set(properties):
            raise ToolContractError("schema_unknown_field")
        for key, child in properties.items():
            if key in value:
                validate_value(value[key], child)
    elif value_type == "array":
        if not isinstance(value, list):
            raise ToolContractError("schema_type_mismatch")
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            raise ToolContractError("schema_array_too_short")
        if maximum is not None and len(value) > maximum:
            raise ToolContractError("schema_array_too_long")
        for item in value:
            validate_value(item, schema["items"])
    elif value_type == "string":
        if not isinstance(value, str):
            raise ToolContractError("schema_type_mismatch")
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < minimum:
            raise ToolContractError("schema_string_too_short")
        if maximum is not None and len(value) > maximum:
            raise ToolContractError("schema_string_too_long")
    elif value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolContractError("schema_type_mismatch")
        _validate_number_bounds(value, schema)
    elif value_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolContractError("schema_type_mismatch")
        _validate_number_bounds(value, schema)
    elif value_type == "boolean":
        if not isinstance(value, bool):
            raise ToolContractError("schema_type_mismatch")
    elif value_type == "null" and value is not None:
        raise ToolContractError("schema_type_mismatch")

    if "enum" in schema and value not in schema["enum"]:
        raise ToolContractError("schema_enum_mismatch")


def _validate_number_bounds(value: int | float, schema: dict[str, Any]) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if minimum is not None and value < minimum:
        raise ToolContractError("schema_number_too_small")
    if maximum is not None and value > maximum:
        raise ToolContractError("schema_number_too_large")


def validate_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(arguments, dict):
        raise ToolContractError("arguments_must_be_object")
    canonical, digest = hash_arguments(arguments)
    validate_value(arguments, schema)
    return canonical, digest


def validate_result(result: Any, schema: dict[str, Any] | None) -> None:
    canonical_json(result, MAX_RESULT_BYTES)
    if schema is not None:
        validate_value(result, schema)
