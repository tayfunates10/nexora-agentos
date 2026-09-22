"""Production PostgreSQL runtime-role grants.

Migrations own the schema. API and worker identities receive only SELECT plus the DML
operations their process is expected to perform. Any unclassified new table makes a
deployment fail until this policy is reviewed.
"""

import re
from collections.abc import Mapping

from psycopg import sql

ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

RUNTIME_TABLES = frozenset(
    {
        "workspaces",
        "workspace_memberships",
        "security_events",
        "agent_definitions",
        "agent_runs",
        "agent_run_events",
        "job_outbox",
        "worker_job_receipts",
        "tool_definitions",
        "tool_policies",
        "tool_calls",
        "tool_approvals",
        "rag_sources",
        "rag_source_acl",
        "rag_chunks",
        "agent_model_steps",
        "knowledge_ingestion_jobs",
        "eval_suites",
        "eval_cases",
        "eval_runs",
        "eval_case_results",
        "eval_agent_run_sources",
        "agent_run_retrievals",
        "agent_run_retrieval_chunks",
        "agent_run_retrieval_context",
        "eval_judge_runs",
        "eval_judge_case_scores",
        "workspace_spend_records",
        "workspace_spend_budgets",
        "workspace_spend_budget_events",
        "workspace_spend_alerts",
        "workspace_spend_alert_outbox",
        "platform_events",
        "catalog_agents",
        "catalog_agent_versions",
        "catalog_agent_entitlements",
        "agent_rollouts",
        "integration_definitions",
        "tenant_integrations",
        "integration_credentials",
        "integration_oauth_states",
        "agent_update_policies",
        "tenant_agents",
        "tenant_agent_version_events",
        "agent_integration_bindings",
    }
)

API_WRITES: Mapping[str, frozenset[str]] = {
    "workspaces": frozenset({"INSERT", "UPDATE"}),
    "workspace_memberships": frozenset({"INSERT", "UPDATE"}),
    "security_events": frozenset({"INSERT"}),
    "agent_definitions": frozenset({"INSERT", "UPDATE"}),
    "agent_runs": frozenset({"INSERT", "UPDATE"}),
    "agent_run_events": frozenset({"INSERT"}),
    "job_outbox": frozenset({"INSERT"}),
    "tool_definitions": frozenset({"INSERT", "UPDATE"}),
    "tool_policies": frozenset({"INSERT", "UPDATE"}),
    "tool_calls": frozenset({"INSERT", "UPDATE"}),
    "tool_approvals": frozenset({"INSERT", "UPDATE"}),
    "rag_sources": frozenset({"INSERT", "UPDATE", "DELETE"}),
    "rag_source_acl": frozenset({"INSERT", "DELETE"}),
    "rag_chunks": frozenset({"INSERT", "DELETE"}),
    "knowledge_ingestion_jobs": frozenset({"INSERT", "UPDATE"}),
    "eval_suites": frozenset({"INSERT"}),
    "eval_cases": frozenset({"INSERT"}),
    "eval_runs": frozenset({"INSERT"}),
    "eval_case_results": frozenset({"INSERT"}),
    "eval_agent_run_sources": frozenset({"INSERT"}),
    "eval_judge_runs": frozenset({"INSERT"}),
    "agent_run_retrieval_context": frozenset({"DELETE"}),
    "workspace_spend_budgets": frozenset({"INSERT", "UPDATE"}),
    "workspace_spend_budget_events": frozenset({"INSERT"}),
    "workspace_spend_alerts": frozenset({"INSERT"}),
    "workspace_spend_alert_outbox": frozenset({"INSERT"}),
    "platform_events": frozenset({"INSERT"}),
    "catalog_agents": frozenset({"INSERT", "UPDATE"}),
    "catalog_agent_versions": frozenset({"INSERT", "UPDATE"}),
    "catalog_agent_entitlements": frozenset({"INSERT", "DELETE"}),
    "agent_rollouts": frozenset({"INSERT", "UPDATE"}),
    "integration_definitions": frozenset({"INSERT", "UPDATE"}),
    "tenant_integrations": frozenset({"INSERT", "UPDATE", "DELETE"}),
    # Rotation writes a new credential and drops the superseded row; no ciphertext is
    # ever edited in place.
    "integration_credentials": frozenset({"INSERT", "UPDATE", "DELETE"}),
    "integration_oauth_states": frozenset({"INSERT", "DELETE"}),
    "agent_update_policies": frozenset({"INSERT", "UPDATE"}),
    "tenant_agents": frozenset({"INSERT", "UPDATE", "DELETE"}),
    "tenant_agent_version_events": frozenset({"INSERT"}),
    "agent_integration_bindings": frozenset({"INSERT", "UPDATE", "DELETE"}),
}

WORKER_WRITES: Mapping[str, frozenset[str]] = {
    "security_events": frozenset({"INSERT"}),
    "agent_definitions": frozenset({"UPDATE"}),
    "agent_runs": frozenset({"UPDATE"}),
    "agent_run_events": frozenset({"INSERT"}),
    "job_outbox": frozenset({"INSERT", "UPDATE"}),
    "worker_job_receipts": frozenset({"INSERT", "UPDATE"}),
    "tool_calls": frozenset({"INSERT", "UPDATE"}),
    "tool_approvals": frozenset({"INSERT", "UPDATE"}),
    "rag_sources": frozenset({"INSERT", "UPDATE", "DELETE"}),
    "rag_source_acl": frozenset({"INSERT", "DELETE"}),
    "rag_chunks": frozenset({"INSERT", "DELETE"}),
    "agent_model_steps": frozenset({"INSERT"}),
    "knowledge_ingestion_jobs": frozenset({"INSERT", "UPDATE"}),
    "agent_run_retrievals": frozenset({"INSERT"}),
    "agent_run_retrieval_chunks": frozenset({"INSERT"}),
    "agent_run_retrieval_context": frozenset({"INSERT", "DELETE"}),
    "eval_judge_runs": frozenset({"UPDATE"}),
    "eval_judge_case_scores": frozenset({"INSERT"}),
    "workspace_spend_records": frozenset({"INSERT"}),
    "workspace_spend_alerts": frozenset({"INSERT"}),
    "workspace_spend_alert_outbox": frozenset({"INSERT", "UPDATE"}),
    # Automatic agent updates are a worker-owned system workflow. It may move only the
    # tenant pin and append its immutable version event; catalog, policy and credentials
    # remain read-only to the worker identity.
    "tenant_agents": frozenset({"UPDATE"}),
    "tenant_agent_version_events": frozenset({"INSERT"}),
    # Connector execution runs in the worker. OAuth refresh and credential rotation stay
    # inside the vault boundary, so the worker needs the same narrowly scoped DML for
    # connection state without any catalog or workspace-management privileges.
    "tenant_integrations": frozenset({"UPDATE"}),
    "integration_credentials": frozenset({"INSERT", "UPDATE", "DELETE"}),
}

WRITE_PRIVILEGES = frozenset({"INSERT", "UPDATE", "DELETE"})
FORBIDDEN_PRIVILEGES = frozenset({"TRUNCATE", "REFERENCES", "TRIGGER"})


def _validate_role_name(value: str, label: str) -> str:
    if not ROLE_RE.fullmatch(value):
        raise RuntimeError(f"{label} must be a simple PostgreSQL role identifier")
    return value


def configured_runtime_roles(
    api_role: str | None,
    worker_role: str | None,
) -> tuple[str, str] | None:
    if api_role is None and worker_role is None:
        return None
    if not api_role or not worker_role:
        raise RuntimeError("Both database_api_role and database_worker_role must be configured")
    api = _validate_role_name(api_role, "database_api_role")
    worker = _validate_role_name(worker_role, "database_worker_role")
    if api == worker:
        raise RuntimeError("API and worker database roles must be different")
    return api, worker


def apply_runtime_grants(connection, api_role: str, worker_role: str) -> None:
    api_role = _validate_role_name(api_role, "database_api_role")
    worker_role = _validate_role_name(worker_role, "database_worker_role")
    if api_role == worker_role:
        raise RuntimeError("API and worker database roles must be different")

    _verify_roles(connection, (api_role, worker_role))
    _verify_schema_inventory(connection)

    # Nexora expects a dedicated database. PUBLIC never receives application-object
    # privileges, and runtime roles can use but cannot create objects in public.
    connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    connection.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC")
    connection.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC")

    for role in (api_role, worker_role):
        identifier = sql.Identifier(role)
        connection.execute(
            sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(
                identifier
            )
        )
        connection.execute(
            sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}").format(
                identifier
            )
        )
        connection.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(identifier))
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(identifier))
        for table in sorted(RUNTIME_TABLES):
            connection.execute(
                sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                    sql.Identifier(table),
                    identifier,
                )
            )

    _grant_writes(connection, api_role, API_WRITES)
    _grant_writes(connection, worker_role, WORKER_WRITES)
    _verify_effective_policy(connection, api_role, API_WRITES)
    _verify_effective_policy(connection, worker_role, WORKER_WRITES)


def _grant_writes(connection, role: str, policy: Mapping[str, frozenset[str]]) -> None:
    identifier = sql.Identifier(role)
    for table, privileges in sorted(policy.items()):
        unknown = privileges - WRITE_PRIVILEGES
        if unknown:
            raise RuntimeError(f"Unsupported runtime privilege for {table}: {sorted(unknown)}")
        connection.execute(
            sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                sql.SQL(", ").join(sql.SQL(privilege) for privilege in sorted(privileges)),
                sql.Identifier(table),
                identifier,
            )
        )


def _verify_roles(connection, roles: tuple[str, str]) -> None:
    rows = connection.execute(
        """SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
           FROM pg_roles WHERE rolname=ANY(%s)""",
        (list(roles),),
    ).fetchall()
    found = {row[0]: row[1:] for row in rows}
    missing = sorted(set(roles) - set(found))
    if missing:
        raise RuntimeError("Configured runtime database roles do not exist: " + ", ".join(missing))
    for role, dangerous in found.items():
        if any(dangerous):
            raise RuntimeError(f"Runtime database role {role} has privileged role attributes")


def _verify_schema_inventory(connection) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ).fetchall()
    }
    application_tables = tables - {"schema_migrations"}
    missing = sorted(RUNTIME_TABLES - application_tables)
    unknown = sorted(application_tables - RUNTIME_TABLES)
    if missing or unknown:
        raise RuntimeError(
            f"Runtime database table policy is stale; missing={missing}, unclassified={unknown}"
        )

    sequences = connection.execute(
        "SELECT sequencename FROM pg_sequences WHERE schemaname='public'"
    ).fetchall()
    if sequences:
        raise RuntimeError(
            "Runtime database sequence policy is undefined: "
            + ", ".join(row[0] for row in sequences)
        )

    current_user = connection.execute("SELECT current_user").fetchone()[0]
    owners = connection.execute(
        """SELECT c.relname,r.rolname
           FROM pg_class c
           JOIN pg_namespace n ON n.oid=c.relnamespace
           JOIN pg_roles r ON r.oid=c.relowner
           WHERE n.nspname='public' AND c.relkind='r'
             AND c.relname=ANY(%s)""",
        (sorted(RUNTIME_TABLES | {"schema_migrations"}),),
    ).fetchall()
    wrongly_owned = sorted(name for name, owner in owners if owner != current_user)
    if wrongly_owned:
        raise RuntimeError(
            "Migration identity must own every Nexora table before role separation: "
            + ", ".join(wrongly_owned)
        )


def _verify_effective_policy(
    connection,
    role: str,
    writes: Mapping[str, frozenset[str]],
) -> None:
    if connection.execute("SELECT has_schema_privilege(%s,'public','CREATE')", (role,)).fetchone()[
        0
    ]:
        raise RuntimeError(f"Runtime database role {role} can create schema objects")

    for table in sorted(RUNTIME_TABLES):
        if not connection.execute(
            "SELECT has_table_privilege(%s,%s,'SELECT')",
            (role, f"public.{table}"),
        ).fetchone()[0]:
            raise RuntimeError(f"Runtime database role {role} cannot read {table}")
        allowed_writes = writes.get(table, frozenset())
        for privilege in sorted(WRITE_PRIVILEGES | FORBIDDEN_PRIVILEGES):
            effective = connection.execute(
                "SELECT has_table_privilege(%s,%s,%s)",
                (role, f"public.{table}", privilege),
            ).fetchone()[0]
            expected = privilege in allowed_writes
            if effective != expected:
                raise RuntimeError(
                    f"Unexpected {privilege} privilege for {role} on {table}: {effective}"
                )
