CREATE TABLE workspace_spend_records (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    -- Deterministic identity of the priced unit of work. It makes the ledger
    -- exactly-once under worker retries and crash recovery.
    source_key text NOT NULL CHECK (length(source_key) BETWEEN 1 AND 200),
    category text NOT NULL CHECK (category IN ('agent_run','evaluation_judge','embedding')),
    provider text NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    model text NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
    input_tokens integer NOT NULL CHECK (input_tokens >= 0),
    output_tokens integer NOT NULL CHECK (output_tokens >= 0),
    -- Cost in micros of the operator accounting currency, priced when the call was
    -- made. Later price changes never rewrite recorded history.
    cost_micros bigint NOT NULL CHECK (cost_micros >= 0),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, source_key)
);
CREATE INDEX workspace_spend_records_period
    ON workspace_spend_records(workspace_id, occurred_at DESC, id DESC);

CREATE TABLE workspace_spend_budgets (
    workspace_id uuid PRIMARY KEY REFERENCES workspaces(id),
    monthly_limit_micros bigint NOT NULL
        CHECK (monthly_limit_micros BETWEEN 0 AND 1000000000000000),
    enforcement text NOT NULL CHECK (enforcement IN ('enforce','monitor')),
    updated_by_issuer text NOT NULL CHECK (length(updated_by_issuer) > 0),
    updated_by_subject text NOT NULL CHECK (length(updated_by_subject) BETWEEN 1 AND 255),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Raising a cap authorizes future spending, so every decision keeps its own record.
CREATE TABLE workspace_spend_budget_events (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    monthly_limit_micros bigint NOT NULL
        CHECK (monthly_limit_micros BETWEEN 0 AND 1000000000000000),
    enforcement text NOT NULL CHECK (enforcement IN ('enforce','monitor')),
    actor_issuer text NOT NULL CHECK (length(actor_issuer) > 0),
    actor_subject text NOT NULL CHECK (length(actor_subject) BETWEEN 1 AND 255),
    request_id text NOT NULL CHECK (length(request_id) BETWEEN 1 AND 64),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX workspace_spend_budget_events_workspace
    ON workspace_spend_budget_events(workspace_id, created_at DESC, id DESC);

CREATE FUNCTION reject_spend_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'spend ledger rows are append-only';
END;
$$;

CREATE TRIGGER workspace_spend_records_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON workspace_spend_records
    FOR EACH STATEMENT EXECUTE FUNCTION reject_spend_ledger_mutation();

CREATE TRIGGER workspace_spend_budget_events_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON workspace_spend_budget_events
    FOR EACH STATEMENT EXECUTE FUNCTION reject_spend_ledger_mutation();
