import asyncio
import base64
import json

import httpx
import pytest

from nexora_api.connector_runtime import ConnectorError, ConnectorRuntime
from nexora_api.integration_manifest import define_integration


async def _host_ok(_host: str, _port: int) -> None:
    return None


def _connector(*, retry="never", idempotency_header=None):
    endpoint = {
        "capability": "posts.create",
        "method": "POST",
        "path": "/posts",
        "side_effect": "external_communication",
        "retry": retry,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 100},
                "status": {"type": "string", "enum": ["draft", "publish"]},
                "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 128},
            },
            "required": ["title", "status", "idempotency_key"],
        },
        "body_map": {"title": "title", "status": "status"},
    }
    if idempotency_header is not None:
        endpoint["idempotency_header"] = idempotency_header
    return define_integration(
        id="content",
        name="Content",
        description="Content API.",
        category="content",
        icon="document",
        version="1.0.0",
        auth="basic",
        capabilities=["posts.create"],
        credential_fields=[
            {"key": "site_url", "label": "Site URL", "secret": False, "required": True},
            {"key": "username", "label": "Username", "secret": False, "required": True},
            {"key": "password", "label": "Password", "secret": True, "required": True},
        ],
        base_url_field="site_url",
        credential_placement={
            "kind": "basic",
            "value_field": "password",
            "username_field": "username",
        },
        endpoints=[endpoint],
    )


def test_runtime_maps_only_declared_fields_and_keeps_credentials_out_of_arguments():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(201, json={"id": 7, "status": "draft"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ConnectorRuntime(client=client, host_validator=_host_ok)
            return await runtime.invoke(
                _connector(),
                "posts.create",
                {"username": "editor", "password": "application-password"},
                {"site_url": "https://site.example.com"},
                arguments={
                    "title": "Search update",
                    "status": "draft",
                    "idempotency_key": "idem-1234",
                },
                idempotency_key="idem-1234",
            )

    result = asyncio.run(run())
    assert result.ok is True
    assert seen == {
        "method": "POST",
        "url": "https://site.example.com/posts",
        "body": {"title": "Search update", "status": "draft"},
        "authorization": "Basic "
        + base64.b64encode(b"editor:application-password").decode(),
    }


def test_runtime_rejects_unknown_agent_fields_before_network_egress():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ConnectorRuntime(client=client, host_validator=_host_ok)
            await runtime.invoke(
                _connector(),
                "posts.create",
                {"username": "editor", "password": "secret"},
                {"site_url": "https://site.example.com"},
                arguments={
                    "title": "Allowed",
                    "status": "draft",
                    "idempotency_key": "idem-1234",
                    "url": "https://attacker.invalid",
                },
                idempotency_key="idem-1234",
            )

    with pytest.raises(ConnectorError, match="connector_invalid_arguments"):
        asyncio.run(run())
    assert calls == 0


def test_idempotent_connector_requires_the_same_durable_key():
    connector = _connector(retry="idempotent", idempotency_header="Idempotency-Key")

    async def run():
        runtime = ConnectorRuntime(host_validator=_host_ok)
        await runtime.invoke(
            connector,
            "posts.create",
            {"username": "editor", "password": "secret"},
            {"site_url": "https://site.example.com"},
            arguments={
                "title": "Allowed",
                "status": "draft",
                "idempotency_key": "idem-1234",
            },
            idempotency_key="different-key",
        )

    with pytest.raises(ConnectorError, match="idempotency_key_mismatch"):
        asyncio.run(run())
