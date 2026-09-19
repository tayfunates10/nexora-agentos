ALTER TABLE agent_runs
    ADD COLUMN attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    ADD COLUMN cancel_requested_at timestamptz,
    ADD COLUMN lease_owner text,
    ADD COLUMN lease_expires_at timestamptz,
    ADD COLUMN finished_at timestamptz,
    ADD COLUMN failure_code text CHECK (
        failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 100
    ),
    ADD CONSTRAINT agent_run_lease_pair CHECK (
        (lease_owner IS NULL) = (lease_expires_at IS NULL)
    );

CREATE TABLE worker_job_receipts (
    job_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    run_id uuid NOT NULL,
    status text NOT NULL CHECK (
        status IN ('processing','succeeded','failed','cancelled','superseded')
    ),
    delivery_count integer NOT NULL DEFAULT 1 CHECK (delivery_count > 0),
    worker_id text,
    lease_expires_at timestamptz,
    last_error_code text CHECK (
        last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 100
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, workspace_id) REFERENCES agent_runs(id, workspace_id)
);
CREATE INDEX worker_job_receipts_run ON worker_job_receipts(run_id, updated_at);

CREATE FUNCTION enforce_agent_run_status_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'queued' AND NEW.status IN ('running','cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'running'
       AND NEW.status IN ('queued','waiting_for_approval','succeeded','failed','cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'waiting_for_approval' AND NEW.status IN ('queued','cancelled') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal agent run transition: % -> %', OLD.status, NEW.status;
END;
$$;

CREATE TRIGGER agent_run_status_transition
    BEFORE UPDATE OF status ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION enforce_agent_run_status_transition();
