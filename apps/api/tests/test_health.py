import asyncio

import pytest
from fastapi.testclient import TestClient

from nexora_api.config import Settings
from nexora_api.health import DependencyProbe, DependencyStatus
from nexora_api.main import create_app


class StubProbe:
    def __init__(self, postgres="up", redis="up"):
        self.status = DependencyStatus(postgres=postgres, redis=redis)

    async def check(self):
        return self.status


@pytest.mark.parametrize(
    "postgres,redis,status",
    [
        ("up", "up", 200),
        ("down", "up", 503),
        ("up", "down", 503),
        ("down", "down", 503),
    ],
)
def test_readiness(postgres, redis, status):
    with TestClient(create_app(probe=StubProbe(postgres, redis))) as client:
        response = client.get("/api/v1/health/ready")
        assert response.status_code == status
        assert response.json()["dependencies"] == {"postgres": postgres, "redis": redis}
        assert client.get("/api/v1/health/live").status_code == 200


@pytest.mark.parametrize(
    "value,accepted", [("req_123", True), ("x" * 65, False), ("bad request", False), ("", False)]
)
def test_request_id(value, accepted):
    with TestClient(create_app(probe=StubProbe())) as client:
        response = client.get("/api/v1/health/live", headers={"x-request-id": value})
        actual = response.headers["x-request-id"]
        assert (actual == value) is accepted
        assert len(actual) <= 64


def test_errors_have_traceable_envelope():
    with TestClient(create_app(probe=StubProbe())) as client:
        response = client.get("/missing")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "http_404"
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def test_internal_error_does_not_leak_details():
    app = create_app(probe=StubProbe())

    @app.get("/broken")
    async def broken():
        raise RuntimeError("postgres://secret-password")

    with TestClient(app) as client:
        response = client.get("/broken")
        assert response.status_code == 500
        assert "secret-password" not in response.text
        assert response.json()["error"]["code"] == "internal_error"


def test_probe_bounds_timeout_and_hides_failures():
    async def run():
        probe = DependencyProbe(Settings(dependency_timeout_seconds=0.01), None)

        async def slow():
            await asyncio.sleep(1)

        async def broken():
            raise RuntimeError("secret-password")

        assert await probe._safe(slow) == "down"
        assert await probe._safe(broken) == "down"

    asyncio.run(run())


def test_openapi_declares_readiness_failure():
    schema = create_app().openapi()
    assert "503" in schema["paths"]["/api/v1/health/ready"]["get"]["responses"]
