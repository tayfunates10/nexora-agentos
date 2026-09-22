-- Runtime projection for standard tenant agents.
--
-- Existing installations created before the action-capable runtime did not have an
-- agent_definitions row, while agent_runs intentionally keep their FK to that durable
-- executor table. Project each installed tenant agent into the existing runtime using the
-- same UUID. Future installs create the projection transactionally in application code.

INSERT INTO agent_definitions (
    id,
    workspace_id,
    name,
    instructions,
    model_profile,
    created_by_issuer,
    created_by_subject,
    origin_catalog_agent_id,
    origin_version_id,
    manifest
)
SELECT
    t.id,
    t.workspace_id,
    t.display_name,
    COALESCE(NULLIF(btrim(t.instructions_override), ''), v.manifest->>'system_instructions'),
    'default',
    'nexora://system',
    'tenant-agent-runtime-migration',
    t.catalog_agent_id,
    t.version_id,
    v.manifest
FROM tenant_agents t
JOIN catalog_agent_versions v ON v.id=t.version_id
WHERE NOT EXISTS (
    SELECT 1
    FROM agent_definitions a
    WHERE a.id=t.id AND a.workspace_id=t.workspace_id
);
