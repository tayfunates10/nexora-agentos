import os

import pytest
from fastapi.testclient import TestClient

from nexora_api.main import create_app


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires local services")
def test_real_dependency_readiness():
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 200, response.text
        assert response.json()["dependencies"] == {"postgres": "up", "redis": "up"}
