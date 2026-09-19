CREATE TABLE agent_definitions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 100),
    instructions text NOT NULL CHECK (length(btrim(instructions)) BETWEEN 1 AND 20000),
    model_profile text NOT NULL DEFAULT 'default'
        CHECK (length(model_profile) BETWEEN 1 AND 64 AND model_profile ~ '^[A-Za-z0-9._-]+$'),
    created_by_issuer text NOT NULL,
    created_by_subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, workspace_id)
);
CREATE INDEX agent_definitions_workspace ON agent_definitions(workspace_id, id);

CREATE TABLE agent_runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    agent_id uuid NOT NULL,
    requested_by_issuer text NOT NULL,
    requested_by_subject text NOT NULL,
    input_text text NOT NULL CHECK (length(btrim(input_text)) BETWEEN 1 AND 20000),
    request_hash text NOT NULL CHECK (length(request_hash) = 64),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    trace_id uuid NOT NULL,
    status text NOT NULL CHECK (
        status IN ('queued','running','waiting_for_approval','succeeded','failed','cancelled')
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, workspace_id),
    UNIQUE (workspace_id, requested_by_issuer, requested_by_subject, idempotency_key),
    FOREIGN KEY (agent_id, workspace_id) REFERENCES agent_definitions(id, workspace_id)
);
CREATE INDEX agent_runs_workspace_time ON agent_runs(workspace_id, created_at, id);
CREATE INDEX agent_runs_agent_time ON agent_runs(agent_id, created_at, id);

CREATE TABLE agent_run_events (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    run_id uuid NOT NULL,
    event_no integer NOT NULL CHECK (event_no > 0),
    event_type text NOT NULL CHECK (length(event_type) BETWEEN 1 AND 100),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, event_no),
    FOREIGN KEY (run_id, workspace_id) REFERENCES agent_runs(id, workspace_id)
);
CREATE INDEX agent_run_events_run_order ON agent_run_events(run_id, event_no);

CREATE FUNCTION reject_agent_run_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'agent_run_events is append-only';
END;
$$;
CREATE TRIGGER agent_run_events_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON agent_run_events
    FOR EACH STATEMENT EXECUTE FUNCTION reject_agent_run_event_mutation();

CREATE TABLE job_outbox (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    run_id uuid NOT NULL,
    topic text NOT NULL CHECK (length(topic) BETWEEN 1 AND 100),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    dead_lettered_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, workspace_id) REFERENCES agent_runs(id, workspace_id)
);
CREATE INDEX job_outbox_pending
    ON job_outbox(available_at, created_at, id)
    WHERE published_at IS NULL AND dead_lettered_at IS NULL;
