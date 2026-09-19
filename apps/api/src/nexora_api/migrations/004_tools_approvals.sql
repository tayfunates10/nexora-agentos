CREATE TABLE workspace_tool_policies (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    tool_name text NOT NULL CHECK (length(tool_name) BETWEEN 3 AND 100),
    decision text NOT NULL CHECK (decision IN ('allow','require_approval','deny')),
    updated_by_issuer text NOT NULL,
    updated_by_subject text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, tool_name)
);

CREATE TABLE tool_calls (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    run_id uuid NOT NULL,
    tool_name text NOT NULL CHECK (length(tool_name) BETWEEN 3 AND 100),
    schema_version integer NOT NULL CHECK (schema_version > 0),
    side_effect text NOT NULL CHECK (
        side_effect IN ('read','write','destructive','external_communication')
    ),
    arguments jsonb NOT NULL CHECK (jsonb_typeof(arguments) = 'object'),
    arguments_hash text NOT NULL CHECK (length(arguments_hash) = 64),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    requested_by_issuer text NOT NULL,
    requested_by_subject text NOT NULL,
    policy_decision text NOT NULL CHECK (
        policy_decision IN ('allow','require_approval','deny')
    ),
    policy_reason text NOT NULL CHECK (length(policy_reason) BETWEEN 1 AND 200),
    status text NOT NULL CHECK (
        status IN (
            'waiting_approval','approved','executing','succeeded','failed',
            'rejected','cancelled','expired','denied'
        )
    ),
    result jsonb CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
    result_hash text CHECK (result_hash IS NULL OR length(result_hash) = 64),
    error_code text CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 100),
    started_at timestamptz,
    finished_at timestamptz,
    duration_ms integer CHECK (duration_ms IS NULL OR duration_ms >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, workspace_id, run_id),
    UNIQUE (
        workspace_id, run_id, requested_by_issuer, requested_by_subject, idempotency_key
    ),
    FOREIGN KEY (run_id, workspace_id) REFERENCES agent_runs(id, workspace_id)
);
CREATE INDEX tool_calls_run_time ON tool_calls(run_id, created_at, id);

CREATE TABLE tool_approvals (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    run_id uuid NOT NULL,
    tool_call_id uuid NOT NULL UNIQUE,
    arguments_hash text NOT NULL CHECK (length(arguments_hash) = 64),
    requested_by_issuer text NOT NULL,
    requested_by_subject text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('pending','approved','rejected','expired','cancelled')
    ),
    policy_reason text NOT NULL CHECK (length(policy_reason) BETWEEN 1 AND 200),
    requested_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    decided_by_issuer text,
    decided_by_subject text,
    decided_at timestamptz,
    decision_reason text CHECK (
        decision_reason IS NULL OR length(decision_reason) BETWEEN 1 AND 500
    ),
    UNIQUE (id, workspace_id, run_id),
    FOREIGN KEY (run_id, workspace_id) REFERENCES agent_runs(id, workspace_id),
    FOREIGN KEY (tool_call_id, workspace_id, run_id)
        REFERENCES tool_calls(id, workspace_id, run_id)
);
CREATE UNIQUE INDEX one_pending_approval_per_run
    ON tool_approvals(run_id) WHERE status='pending';
CREATE INDEX tool_approvals_workspace_status
    ON tool_approvals(workspace_id, status, requested_at, id);

CREATE FUNCTION enforce_tool_call_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.tool_name IS DISTINCT FROM OLD.tool_name
       OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
       OR NEW.side_effect IS DISTINCT FROM OLD.side_effect
       OR NEW.arguments IS DISTINCT FROM OLD.arguments
       OR NEW.arguments_hash IS DISTINCT FROM OLD.arguments_hash
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.requested_by_issuer IS DISTINCT FROM OLD.requested_by_issuer
       OR NEW.requested_by_subject IS DISTINCT FROM OLD.requested_by_subject THEN
        RAISE EXCEPTION 'tool call contract is immutable';
    END IF;

    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'waiting_approval'
       AND NEW.status IN ('approved','rejected','expired','cancelled','denied') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status IN ('executing','denied','cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'executing' AND NEW.status IN ('succeeded','failed','cancelled') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal tool call transition: % -> %', OLD.status, NEW.status;
END;
$$;
CREATE TRIGGER tool_calls_update_guard
    BEFORE UPDATE ON tool_calls
    FOR EACH ROW EXECUTE FUNCTION enforce_tool_call_update();

CREATE FUNCTION enforce_tool_approval_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.tool_call_id IS DISTINCT FROM OLD.tool_call_id
       OR NEW.arguments_hash IS DISTINCT FROM OLD.arguments_hash
       OR NEW.requested_by_issuer IS DISTINCT FROM OLD.requested_by_issuer
       OR NEW.requested_by_subject IS DISTINCT FROM OLD.requested_by_subject
       OR NEW.policy_reason IS DISTINCT FROM OLD.policy_reason
       OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'approval request is immutable';
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'pending'
       AND NEW.status IN ('approved','rejected','expired','cancelled') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal approval transition: % -> %', OLD.status, NEW.status;
END;
$$;
CREATE TRIGGER tool_approvals_update_guard
    BEFORE UPDATE ON tool_approvals
    FOR EACH ROW EXECUTE FUNCTION enforce_tool_approval_update();

CREATE FUNCTION reject_tool_audit_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'tool audit records cannot be deleted';
END;
$$;
CREATE TRIGGER tool_calls_no_delete
    BEFORE DELETE OR TRUNCATE ON tool_calls
    FOR EACH STATEMENT EXECUTE FUNCTION reject_tool_audit_delete();
CREATE TRIGGER tool_approvals_no_delete
    BEFORE DELETE OR TRUNCATE ON tool_approvals
    FOR EACH STATEMENT EXECUTE FUNCTION reject_tool_audit_delete();
