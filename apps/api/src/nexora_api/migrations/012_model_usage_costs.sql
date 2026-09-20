CREATE TABLE model_usage_costs (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    source_kind text NOT NULL CHECK (
        source_kind IN ('agent_model_step','eval_judge_candidate','eval_judge_baseline')
    ),
    run_id uuid,
    step_no integer CHECK (step_no IS NULL OR step_no BETWEEN 0 AND 31),
    judge_run_id uuid,
    eval_run_id uuid,
    case_id uuid,
    provider text NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    model text NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
    pricing_version text NOT NULL CHECK (length(pricing_version) BETWEEN 1 AND 100),
    input_tokens integer NOT NULL CHECK (input_tokens >= 0),
    output_tokens integer NOT NULL CHECK (output_tokens >= 0),
    input_usd_micros_per_million_tokens bigint NOT NULL CHECK (
        input_usd_micros_per_million_tokens >= 0
    ),
    output_usd_micros_per_million_tokens bigint NOT NULL CHECK (
        output_usd_micros_per_million_tokens >= 0
    ),
    total_usd_picos numeric(30,0) GENERATED ALWAYS AS (
        input_tokens::numeric * input_usd_micros_per_million_tokens
        + output_tokens::numeric * output_usd_micros_per_million_tokens
    ) STORED,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (
            source_kind='agent_model_step'
            AND run_id IS NOT NULL
            AND step_no IS NOT NULL
            AND judge_run_id IS NULL
            AND eval_run_id IS NULL
            AND case_id IS NULL
        )
        OR
        (
            source_kind IN ('eval_judge_candidate','eval_judge_baseline')
            AND run_id IS NULL
            AND step_no IS NULL
            AND judge_run_id IS NOT NULL
            AND eval_run_id IS NOT NULL
            AND case_id IS NOT NULL
        )
    ),
    FOREIGN KEY (run_id,workspace_id) REFERENCES agent_runs(id,workspace_id),
    FOREIGN KEY (judge_run_id,workspace_id,eval_run_id)
        REFERENCES eval_judge_runs(id,workspace_id,eval_run_id),
    FOREIGN KEY (eval_run_id,case_id) REFERENCES eval_case_results(eval_run_id,case_id)
);

CREATE UNIQUE INDEX model_usage_costs_agent_step
    ON model_usage_costs(run_id,step_no)
    WHERE source_kind='agent_model_step';

CREATE UNIQUE INDEX model_usage_costs_judge_case
    ON model_usage_costs(judge_run_id,case_id,source_kind)
    WHERE source_kind IN ('eval_judge_candidate','eval_judge_baseline');

CREATE INDEX model_usage_costs_workspace_created
    ON model_usage_costs(workspace_id,created_at,provider,model);

CREATE FUNCTION reject_model_usage_cost_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'model usage cost snapshots are append-only';
END;
$$;

CREATE TRIGGER model_usage_costs_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON model_usage_costs
    FOR EACH STATEMENT EXECUTE FUNCTION reject_model_usage_cost_mutation();
