-- Let one durable run target either a workspace-authored agent or an installed
-- standard-agent instance without collapsing the two ownership/lifecycle models.
--
-- agent_id remains the public target identifier so every existing API/event/result
-- contract stays stable. The typed target columns restore referential integrity, while
-- agent_snapshot freezes the exact published version and connector bindings a queued run
-- is allowed to use.
ALTER TABLE agent_runs
    DROP CONSTRAINT agent_runs_agent_id_workspace_id_fkey,
    ADD COLUMN custom_agent_id uuid,
    ADD COLUMN tenant_agent_id uuid,
    ADD COLUMN agent_snapshot jsonb
        CHECK (agent_snapshot IS NULL OR jsonb_typeof(agent_snapshot) = 'object');

UPDATE agent_runs SET custom_agent_id = agent_id;

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_custom_agent_fk
        FOREIGN KEY (custom_agent_id, workspace_id)
        REFERENCES agent_definitions(id, workspace_id),
    ADD CONSTRAINT agent_runs_tenant_agent_fk
        FOREIGN KEY (tenant_agent_id, workspace_id)
        REFERENCES tenant_agents(id, workspace_id),
    ADD CONSTRAINT agent_runs_exactly_one_target CHECK (
        (
            custom_agent_id IS NOT NULL
            AND tenant_agent_id IS NULL
            AND agent_snapshot IS NULL
            AND agent_id = custom_agent_id
        )
        OR
        (
            custom_agent_id IS NULL
            AND tenant_agent_id IS NOT NULL
            AND agent_snapshot IS NOT NULL
            AND agent_id = tenant_agent_id
        )
    );

CREATE INDEX agent_runs_tenant_agent_time
    ON agent_runs(tenant_agent_id, created_at, id)
    WHERE tenant_agent_id IS NOT NULL;
