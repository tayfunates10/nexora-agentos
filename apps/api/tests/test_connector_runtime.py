import json
from pathlib import Path

from nexora_api.connector_runtime import ConnectorRuntime
from nexora_api.integration_manifest import define_integration, load_connector


def mutation_endpoint(path="/repos/{owner}/{repo}/issues"):
    connector = define_integration(
        id="github",
        name="GitHub",
        description="GitHub test connector.",
        category="engineering",
        icon="code",
        version="1.1.0",
        auth="bearer_token",
        capabilities=["issues.write"],
        credential_fields=[
            {"key": "access_token", "label": "Token", "secret": True, "required": True}
        ],
        base_url="https://api.github.com",
        credential_placement={"kind": "bearer_header", "value_field": "access_token"},
        endpoints=[
            {
                "capability": "issues.write",
                "method": "POST",
                "path": path,
                "side_effect": "write",
                "retry": "never",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "title": {"type": "string"},
                        "idempotency_key": {"type": "string"},
                    },
                    "required": ["owner", "repo", "title", "idempotency_key"],
                    "additionalProperties": False,
                },
            }
        ],
    )
    return connector.endpoint_for("issues.write")


def test_mutation_arguments_become_path_body_and_idempotency_header_value():
    endpoint = mutation_endpoint()
    path, params, body, idempotency = ConnectorRuntime._request_parts(
        endpoint,
        {},
        {
            "owner": "acme corp",
            "repo": "ops/api",
            "title": "Production incident",
            "idempotency_key": "run-12345678",
        },
    )

    assert path == "/repos/acme%20corp/ops%2Fapi/issues"
    assert params == {}
    assert body == {"title": "Production incident"}
    assert idempotency == "run-12345678"


def test_connector_config_resolves_path_without_exposing_config_to_body():
    endpoint = mutation_endpoint("/v1/{account_id}/issues")
    path, params, body, idempotency = ConnectorRuntime._request_parts(
        endpoint,
        {"account_id": "business/42"},
        {
            "owner": "unused",
            "repo": "unused",
            "title": "Hello",
            "idempotency_key": "run-abcdefgh",
        },
    )

    assert path == "/v1/business%2F42/issues"
    assert "account_id" not in body
    assert idempotency == "run-abcdefgh"


def test_shipped_write_connectors_require_idempotency_and_are_not_read_effects():
    repository = Path(__file__).resolve().parents[3]
    expected = {
        "github": "issues.write",
        "whatsapp": "messages.send",
        "mikro": "orders.write",
        "custom-rest": "request.write",
        "instagram": "comments.reply",
    }
    for connector_id, capability in expected.items():
        document = json.loads(
            (repository / "connectors" / connector_id / "connector.json").read_text()
        )
        endpoint = load_connector(document).endpoint_for(capability)
        assert endpoint is not None
        assert endpoint.side_effect != "read"
        assert endpoint.retry == "never"
        assert "idempotency_key" in endpoint.input_schema["required"]


def test_side_effecting_endpoint_cannot_use_safe_retry():
    document = {
        "id": "writer",
        "name": "Writer",
        "description": "Writer connector.",
        "category": "custom",
        "icon": "plug",
        "version": "1.0.0",
        "auth": "bearer_token",
        "capabilities": ["records.write"],
        "credential_fields": [
            {"key": "access_token", "label": "Token", "secret": True, "required": True}
        ],
        "base_url": "https://api.example.com",
        "credential_placement": {"kind": "bearer_header", "value_field": "access_token"},
        "endpoints": [
            {
                "capability": "records.write",
                "method": "POST",
                "path": "/records",
                "side_effect": "write",
                "retry": "safe",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "idempotency_key": {"type": "string", "minLength": 8},
                    },
                    "required": ["idempotency_key"],
                    "additionalProperties": False,
                },
            }
        ],
    }

    try:
        load_connector(document)
    except ValueError as exc:
        assert "safe retries" in str(exc)
    else:
        raise AssertionError("unsafe mutation retry contract was accepted")
