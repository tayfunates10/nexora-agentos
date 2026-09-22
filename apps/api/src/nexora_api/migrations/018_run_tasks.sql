-- Durable per-run task graph for plans, follow-ups and grounded verification.
CREATE TABLE run_tasks (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    run_id uuid NOT NULL,
    parent_task_id uuid,
    kind text NOT NULL CHECK (kind IN ('goal','follow_up')),
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 300),
    description text NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
    status text NOT NULL CHECK (status IN ('planned','running','succeeded','failed','cancelled','blocked')),
    action_tool_name text CHECK (action_tool_name IS NULL OR length(action_tool_name) BETWEEN 2 AND 64),
    verification_state text NOT NULL CHECK (verification_state IN ('not_required','pending','verified','failed')),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    request_hash text NOT NULL CHECK (length(request_hash) = 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (id,workspace_id,run_id),
    UNIQUE (run_id,idempotency_key),
    FOREIGN KEY (run_id,workspace_id) REFERENCES agent_runs(id,workspace_id),
    FOREIGN KEY (parent_task_id,workspace_id,run_id) REFERENCES run_tasks(id,workspace_id,run_id)
);
CREATE INDEX run_tasks_run_order ON run_tasks(run_id,created_at,id);
CREATE INDEX run_tasks_parent ON run_tasks(parent_task_id) WHERE parent_task_id IS NOT NULL;

CREATE TABLE run_task_dependencies (
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    task_id uuid NOT NULL,
    depends_on_task_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id,depends_on_task_id),
    FOREIGN KEY (task_id,workspace_id,run_id) REFERENCES run_tasks(id,workspace_id,run_id),
    FOREIGN KEY (depends_on_task_id,workspace_id,run_id) REFERENCES run_tasks(id,workspace_id,run_id),
    CHECK (task_id <> depends_on_task_id)
);
CREATE INDEX run_task_dependencies_run ON run_task_dependencies(run_id,task_id);

CREATE TABLE run_task_evidence (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    run_id uuid NOT NULL,
    task_id uuid NOT NULL,
    action_tool_call_id uuid,
    verification_tool_call_id uuid NOT NULL,
    verification_tool_name text NOT NULL CHECK (length(verification_tool_name) BETWEEN 2 AND 64),
    summary text NOT NULL CHECK (length(btrim(summary)) BETWEEN 1 AND 2000),
    satisfied boolean NOT NULL,
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id,idempotency_key),
    FOREIGN KEY (task_id,workspace_id,run_id) REFERENCES run_tasks(id,workspace_id,run_id),
    FOREIGN KEY (action_tool_call_id,workspace_id,run_id) REFERENCES tool_calls(id,workspace_id,run_id),
    FOREIGN KEY (verification_tool_call_id,workspace_id,run_id) REFERENCES tool_calls(id,workspace_id,run_id)
);
CREATE INDEX run_task_evidence_task ON run_task_evidence(task_id,created_at,id);

INSERT INTO run_tasks
    (id,workspace_id,run_id,parent_task_id,kind,title,description,status,
     action_tool_name,verification_state,idempotency_key,request_hash,created_at,updated_at,completed_at)
SELECT r.id,r.workspace_id,r.id,NULL,'goal','Run goal',left(r.input_text,2000),
    CASE r.status WHEN 'queued' THEN 'planned' WHEN 'running' THEN 'running'
      WHEN 'waiting_for_approval' THEN 'running' WHEN 'succeeded' THEN 'succeeded'
      WHEN 'failed' THEN 'failed' WHEN 'cancelled' THEN 'cancelled' END,
    NULL,'not_required','goal:' || r.id::text,r.request_hash,r.created_at,r.updated_at,
    CASE WHEN r.status IN ('succeeded','failed','cancelled') THEN r.finished_at ELSE NULL END
FROM agent_runs r WHERE r.tenant_agent_id IS NOT NULL
ON CONFLICT (id) DO NOTHING;

CREATE FUNCTION sync_run_goal_task_status() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN RETURN NEW; END IF;
    UPDATE run_tasks SET
      status=CASE NEW.status WHEN 'queued' THEN 'planned' WHEN 'running' THEN 'running'
        WHEN 'waiting_for_approval' THEN 'running' WHEN 'succeeded' THEN 'succeeded'
        WHEN 'failed' THEN 'failed' WHEN 'cancelled' THEN 'cancelled' END,
      completed_at=CASE WHEN NEW.status IN ('succeeded','failed','cancelled') THEN NEW.finished_at ELSE NULL END,
      updated_at=now()
    WHERE id=NEW.id AND run_id=NEW.id AND kind='goal';
    RETURN NEW;
END;
$$;
CREATE TRIGGER agent_run_goal_task_status AFTER UPDATE OF status ON agent_runs
FOR EACH ROW EXECUTE FUNCTION sync_run_goal_task_status();

CREATE FUNCTION reject_run_task_evidence_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'run_task_evidence is append-only'; END;
$$;
CREATE TRIGGER run_task_evidence_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON run_task_evidence
FOR EACH STATEMENT EXECUTE FUNCTION reject_run_task_evidence_mutation();
