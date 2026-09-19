import pytest

from nexora_api.tool_contracts import (
    ToolContractError,
    hash_tool_contract,
    validate_arguments,
    validate_registration_schema,
    validate_result,
)


def strict_schema():
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "required": ["query"],
        "additionalProperties": False,
    }


def test_strict_tool_schema_accepts_and_hashes_normalized_arguments():
    schema = strict_schema()
    validate_registration_schema(schema, side_effect="read")
    first = validate_arguments({"query": "status"}, schema)
    second = validate_arguments({"query": "status"}, schema)
    assert first == second
    assert len(first[1]) == 64


def test_unknown_fields_are_rejected():
    schema = strict_schema()
    with pytest.raises(ToolContractError) as exc:
        validate_arguments({"query": "status", "escape": "no"}, schema)
    assert exc.value.code == "schema_unknown_field"


def test_mutating_tools_require_idempotency_key_contract():
    with pytest.raises(ToolContractError) as exc:
        validate_registration_schema(strict_schema(), side_effect="write")
    assert exc.value.code == "mutation_requires_idempotency_key"

    schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "minLength": 1},
            "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
        },
        "required": ["message", "idempotency_key"],
        "additionalProperties": False,
    }
    validate_registration_schema(schema, side_effect="external_communication")


def test_input_schema_must_explicitly_forbid_unknown_fields():
    schema = strict_schema()
    del schema["additionalProperties"]
    with pytest.raises(ToolContractError) as exc:
        validate_registration_schema(schema, side_effect="read")
    assert exc.value.code == "input_schema_must_forbid_unknown_fields"


def test_output_contract_is_enforced():
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer", "minimum": 0}},
        "required": ["count"],
        "additionalProperties": False,
    }
    validate_registration_schema(schema, output=True)
    validate_result({"count": 2}, schema)
    with pytest.raises(ToolContractError) as exc:
        validate_result({"count": -1}, schema)
    assert exc.value.code == "schema_number_too_small"


def test_tool_contract_hash_binds_execution_target_and_schema():
    schema = strict_schema()
    first = hash_tool_contract(
        server_key="primary",
        remote_name="lookup",
        input_schema=schema,
        output_schema=None,
        side_effect="read",
    )
    same = hash_tool_contract(
        server_key="primary",
        remote_name="lookup",
        input_schema=schema,
        output_schema=None,
        side_effect="read",
    )
    changed_target = hash_tool_contract(
        server_key="primary",
        remote_name="delete",
        input_schema=schema,
        output_schema=None,
        side_effect="read",
    )
    assert first == same
    assert first != changed_target


def test_registration_rejects_invalid_constraint_types():
    schema = strict_schema()
    schema["properties"]["query"]["maxLength"] = "100"
    with pytest.raises(ToolContractError) as exc:
        validate_registration_schema(schema, side_effect="read")
    assert exc.value.code == "invalid_schema_size_bound"


def test_registration_rejects_excessive_schema_depth():
    leaf = {"type": "string"}
    for _ in range(18):
        leaf = {"type": "array", "items": leaf}
    schema = {
        "type": "object",
        "properties": {"nested": leaf},
        "required": ["nested"],
        "additionalProperties": False,
    }
    with pytest.raises(ToolContractError) as exc:
        validate_registration_schema(schema, side_effect="read")
    assert exc.value.code == "schema_too_deep"
