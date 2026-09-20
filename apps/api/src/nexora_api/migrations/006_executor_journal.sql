CREATE TABLE agent_model_steps (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    step_no integer NOT NULL CHECK (step_no BETWEEN 0 AND 31),
    provider text NOT NULL,
    model text NOT NULL,
    routing_reason text NOT NULL,
    response jsonb NOT NULL CHECK (jsonb_typeof(response) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, step_no),
    FOREIGN KEY (run_id, workspace_id) REFERENCES agent_runs(id, workspace_id)
);
CREATE TRIGGER agent_model_steps_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON agent_model_steps
    FOR EACH STATEMENT EXECUTE FUNCTION reject_agent_run_event_mutation();
