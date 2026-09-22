-- Direct standard-agent runs keep their catalog identity separate from custom agents.
-- The snapshot freezes every execution-relevant tenant-agent field at queue time so a
-- later catalog rollout, prompt edit or integration rebinding cannot redirect a queued
-- or retried run.

ALTER TABLE agent_runs
    ALTER COLUMN agent_id DROP NOT NULL,
    ADD COLUMN tenant_agent_id uuid,
    ADD COLUMN agent_snapshot jsonb;

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_exactly_one_agent_target
        CHECK ((agent_id IS NOT NULL) <> (tenant_agent_id IS NOT NULL)),
    ADD CONSTRAINT agent_runs_tenant_agent_fk
        FOREIGN KEY (tenant_agent_id, workspace_id)
        REFERENCES tenant_agents(id, workspace_id),
    ADD CONSTRAINT agent_runs_standard_snapshot
        CHECK (
            tenant_agent_id IS NULL
            OR (
                agent_snapshot IS NOT NULL
                AND jsonb_typeof(agent_snapshot) = 'object'
            )
        );

CREATE INDEX agent_runs_tenant_agent_time
    ON agent_runs(tenant_agent_id, created_at, id)
    WHERE tenant_agent_id IS NOT NULL;
