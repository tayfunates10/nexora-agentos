import uuid

import psycopg
import pytest

from nexora_api.config import Settings
from nexora_api.database_roles import (
    API_WRITES,
    RUNTIME_TABLES,
    WORKER_WRITES,
    apply_runtime_grants,
    configured_runtime_roles,
)


def test_runtime_role_configuration_is_all_or_nothing():
    assert configured_runtime_roles(None, None) is None
    with pytest.raises(RuntimeError, match="Both"):
        configured_runtime_roles("nexora_api_runtime", None)
    with pytest.raises(RuntimeError, match="different"):
        configured_runtime_roles("same_role", "same_role")
    with pytest.raises(RuntimeError, match="identifier"):
        configured_runtime_roles("unsafe-role", "worker_role")


def has_table_privilege(connection, role, table, privilege):
    return connection.execute(
        "SELECT has_table_privilege(%s,%s,%s)",
        (role, f"public.{table}", privilege),
    ).fetchone()[0]


@pytest.mark.integration
def test_runtime_roles_are_effectively_least_privilege():
    settings = Settings()
    api_role = "api_test_" + uuid.uuid4().hex[:12]
    worker_role = "worker_test_" + uuid.uuid4().hex[:12]

    with psycopg.connect(settings.database_url.get_secret_value()) as connection:
        connection.execute(f'CREATE ROLE "{api_role}" NOLOGIN')
        connection.execute(f'CREATE ROLE "{worker_role}" NOLOGIN')
        try:
            apply_runtime_grants(connection, api_role, worker_role)

            for role, writes in ((api_role, API_WRITES), (worker_role, WORKER_WRITES)):
                can_create = connection.execute(
                    "SELECT has_schema_privilege(%s,'public','CREATE')",
                    (role,),
                ).fetchone()[0]
                assert can_create is False

                for table in RUNTIME_TABLES:
                    assert has_table_privilege(connection, role, table, "SELECT") is True
                    for privilege in ("INSERT", "UPDATE", "DELETE"):
                        expected = privilege in writes.get(table, frozenset())
                        assert has_table_privilege(connection, role, table, privilege) is expected
                    for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                        assert has_table_privilege(connection, role, table, privilege) is False

            assert has_table_privilege(
                connection, api_role, "worker_job_receipts", "INSERT"
            ) is False
            assert has_table_privilege(
                connection, api_role, "agent_model_steps", "INSERT"
            ) is False
            assert has_table_privilege(connection, worker_role, "workspaces", "UPDATE") is False
            assert has_table_privilege(connection, worker_role, "eval_suites", "INSERT") is False
        finally:
            connection.execute(f'DROP OWNED BY "{api_role}"')
            connection.execute(f'DROP OWNED BY "{worker_role}"')
            connection.execute(f'DROP ROLE "{api_role}"')
            connection.execute(f'DROP ROLE "{worker_role}"')
