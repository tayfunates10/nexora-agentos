import base64
import json
import os

import pytest
from test_auth import auth_settings, keys  # noqa: F401

from nexora_api.config import Settings

PLATFORM_ADMIN = "nexora-platform-admin"


@pytest.fixture
def vault_keys():
    """Two master keys so rotation can be exercised end to end."""
    return {
        "test-key-1": base64.b64encode(b"\x11" * 32).decode(),
        "test-key-2": base64.b64encode(b"\x22" * 32).decode(),
    }


@pytest.fixture
def platform_settings(keys, vault_keys):  # noqa: F811
    """Auth settings with a configured vault and one platform administrator."""
    return Settings(
        auth_issuer="https://identity.example.test/",
        auth_audience="nexora-api",
        auth_public_key=keys[1],
        secret_vault_keys=json.dumps(vault_keys),
        secret_vault_active_key="test-key-1",
        platform_admin_subjects=PLATFORM_ADMIN,
        database_url=os.environ.get("NEXORA_DATABASE_URL", "postgresql://localhost/nexora"),
    )
