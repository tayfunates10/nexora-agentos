CREATE TABLE tool_definitions (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    name text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_.-]{1,63}$'),
    server_key text NOT NULL CHECK (server_key ~ '^[a-z][a-z0-9_.-]{1,63}$'),
    remote_name text NOT NULL CHECK (length(remote_name) BETWEEN 1 AND 128),
    description text NOT NULL CHECK (length(description) BETWEEN 1 AND 1000),
    input_schema jsonb NOT NULL CHECK (jsonb_typeof(input_schema) = 'object'),
    output_schema jsonb CHECK (
        output_schema IS NULL OR jsonb_typeof(output_schema) = 'object'
    ),
    side_effect text NOT NULL CHECK (
        side_effect IN ('read','write','destructive','external_communication')
    ),
    enabled boolean NOT NULL DEFAULT true,
    created_by_issuer text NOT NULL,
    created_by_subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name),
    UNIQUE (id, workspace_id)
);
CREATE INDEX tool_definitions_workspace ON tool_definitions(workspace_id, name);

CREATE TABLE tool_policies (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    tool_id uuid NOT NULL,
    decision text NOT NULL CHECK (decision IN ('allow','deny','require_approval')),
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 500),
    updated_by_issuer text NOT NULL,
    updated_by_subject text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, tool_id),
    FOREIGN KEY (tool_id, workspace_id) REFERENCES tool_definitions(id, workspace_id)
);

CREATE TABLE tool_calls (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    run_id uuid NOT NULL,
    tool_id uuid NOT NULL,
    call_key text NOT NULL CHECK (length(call_key) BETWEEN 1 AND 128),
    arguments jsonb NOT NULL CHECK (jsonb_typeof(arguments) = 'object'),
    arguments_hash text NOT NULL CHECK (length(arguments_hash) = 64),
    contract_hash text NOT NULL CHECK (length(contract_hash) = 64),
    status text NOT NULL CHECK (
        status IN (
            'planned','pending_approval','approved','running','succeeded',
            'failed','denied','cancelled'
        )
    ),
    policy_decision text NOT NULL CHECK (
        policy_decision IN ('allow','deny','require_approval')
    ),
    result jsonb,
    error_code text CHECK (
        error_code IS NULL OR length(error_code) BETWEEN 1 AND 100
    ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    UNIQUE (run_id, call_key),
    UNIQUE (id, workspace_id, run_id),
    FOREIGN KEY (run_id, workspace_id) REFERENCES agent_runs(id, workspace_id),
    FOREIGN KEY (tool_id, workspace_id) REFERENCES tool_definitions(id, workspace_id),
    CHECK (result IS NULL OR jsonb_typeof(result) IN ('object','array','string','number','boolean','null'))
);
CREATE INDEX tool_calls_run ON tool_calls(run_id, created_at, id);

CREATE TABLE tool_approvals (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    run_id uuid NOT NULL,
    tool_call_id uuid NOT NULL UNIQUE,
    requested_action text NOT NULL CHECK (length(requested_action) BETWEEN 1 AND 128),
    normalized_arguments jsonb NOT NULL CHECK (jsonb_typeof(normalized_arguments) = 'object'),
    arguments_hash text NOT NULL CHECK (length(arguments_hash) = 64),
    contract_hash text NOT NULL CHECK (length(contract_hash) = 64),
    requester_issuer text NOT NULL,
    requester_subject text NOT NULL,
    approver_issuer text,
    approver_subject text,
    status text NOT NULL CHECK (
        status IN ('pending','approved','rejected','expired','cancelled')
    ),
    policy_reason text NOT NULL CHECK (length(policy_reason) BETWEEN 1 AND 500),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    decided_at timestamptz,
    FOREIGN KEY (tool_call_id, workspace_id, run_id)
        REFERENCES tool_calls(id, workspace_id, run_id)
);
CREATE INDEX tool_approvals_pending
    ON tool_approvals(workspace_id, expires_at, created_at)
    WHERE status='pending';

CREATE FUNCTION enforce_tool_call_status_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'planned' AND NEW.status IN ('pending_approval','running','denied','cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'pending_approval'
       AND NEW.status IN ('approved','denied','cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status IN ('running','cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'running'
       AND NEW.status IN ('planned','approved','succeeded','failed','cancelled') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal tool call transition: % -> %', OLD.status, NEW.status;
END;
$$;
CREATE TRIGGER tool_call_status_transition
    BEFORE UPDATE OF status ON tool_calls
    FOR EACH ROW EXECUTE FUNCTION enforce_tool_call_status_transition();

CREATE FUNCTION protect_tool_approval() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.tool_call_id IS DISTINCT FROM OLD.tool_call_id
       OR NEW.requested_action IS DISTINCT FROM OLD.requested_action
       OR NEW.normalized_arguments IS DISTINCT FROM OLD.normalized_arguments
       OR NEW.arguments_hash IS DISTINCT FROM OLD.arguments_hash
       OR NEW.contract_hash IS DISTINCT FROM OLD.contract_hash
       OR NEW.requester_issuer IS DISTINCT FROM OLD.requester_issuer
       OR NEW.requester_subject IS DISTINCT FROM OLD.requester_subject
       OR NEW.policy_reason IS DISTINCT FROM OLD.policy_reason
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'tool approval request content is immutable';
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'pending'
       AND NEW.status IN ('approved','rejected','expired','cancelled') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal tool approval transition: % -> %', OLD.status, NEW.status;
END;
$$;
CREATE TRIGGER tool_approval_protected
    BEFORE UPDATE ON tool_approvals
    FOR EACH ROW EXECUTE FUNCTION protect_tool_approval();

CREATE FUNCTION reject_tool_approval_deletion() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'tool approval records are durable';
END;
$$;
CREATE TRIGGER tool_approval_no_delete
    BEFORE DELETE OR TRUNCATE ON tool_approvals
    FOR EACH STATEMENT EXECUTE FUNCTION reject_tool_approval_deletion();
