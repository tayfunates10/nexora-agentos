CREATE TABLE eval_agent_run_sources (
    eval_run_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    case_id uuid NOT NULL,
    agent_run_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (eval_run_id, case_id),
    UNIQUE (eval_run_id, agent_run_id),
    FOREIGN KEY (eval_run_id, workspace_id) REFERENCES eval_runs(id, workspace_id),
    FOREIGN KEY (case_id, workspace_id) REFERENCES eval_cases(id, workspace_id),
    FOREIGN KEY (eval_run_id, case_id) REFERENCES eval_case_results(eval_run_id, case_id),
    FOREIGN KEY (agent_run_id, workspace_id) REFERENCES agent_runs(id, workspace_id)
);
CREATE INDEX eval_agent_run_sources_agent
    ON eval_agent_run_sources(workspace_id, agent_run_id);

CREATE TRIGGER eval_agent_run_sources_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON eval_agent_run_sources
    FOR EACH STATEMENT EXECUTE FUNCTION reject_evaluation_mutation();
