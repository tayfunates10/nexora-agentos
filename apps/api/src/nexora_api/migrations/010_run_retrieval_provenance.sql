CREATE TABLE agent_run_retrievals (
    run_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    query_hash text NOT NULL CHECK (length(query_hash) = 64),
    context_hash text NOT NULL CHECK (length(context_hash) = 64),
    embedding_input_tokens integer NOT NULL CHECK (embedding_input_tokens >= 0),
    chunk_count integer NOT NULL CHECK (chunk_count BETWEEN 0 AND 50),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, workspace_id),
    FOREIGN KEY (run_id, workspace_id) REFERENCES agent_runs(id, workspace_id)
);

CREATE TABLE agent_run_retrieval_chunks (
    run_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    position integer NOT NULL CHECK (position BETWEEN 0 AND 49),
    chunk_id uuid NOT NULL,
    source_id uuid NOT NULL,
    source_key text NOT NULL CHECK (length(source_key) BETWEEN 1 AND 255),
    source_version text NOT NULL CHECK (length(source_version) BETWEEN 1 AND 128),
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    content_hash text NOT NULL CHECK (length(content_hash) = 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, position),
    UNIQUE (run_id, chunk_id),
    FOREIGN KEY (run_id, workspace_id)
        REFERENCES agent_run_retrievals(run_id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX agent_run_retrieval_chunks_source
    ON agent_run_retrieval_chunks(workspace_id, source_key, source_version);

-- Exact retrieved text is needed only while a run may retry or resume from approval.
-- Durable evaluation provenance lives in the hash/identifier tables above.
CREATE TABLE agent_run_retrieval_context (
    run_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    context_text text NOT NULL CHECK (length(context_text) <= 200000),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, workspace_id)
        REFERENCES agent_run_retrievals(run_id, workspace_id) ON DELETE CASCADE
);

CREATE FUNCTION reject_agent_run_retrieval_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'agent run retrieval provenance is append-only';
END;
$$;

CREATE TRIGGER agent_run_retrievals_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON agent_run_retrievals
    FOR EACH STATEMENT EXECUTE FUNCTION reject_agent_run_retrieval_mutation();
CREATE TRIGGER agent_run_retrieval_chunks_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON agent_run_retrieval_chunks
    FOR EACH STATEMENT EXECUTE FUNCTION reject_agent_run_retrieval_mutation();

CREATE FUNCTION reject_agent_run_retrieval_context_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'agent run retrieval context is immutable until terminal cleanup';
END;
$$;

CREATE TRIGGER agent_run_retrieval_context_no_update
    BEFORE UPDATE OR TRUNCATE ON agent_run_retrieval_context
    FOR EACH STATEMENT EXECUTE FUNCTION reject_agent_run_retrieval_context_update();

CREATE FUNCTION clear_terminal_agent_run_retrieval_context() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IN ('succeeded','failed','cancelled') AND NEW.status <> OLD.status THEN
        DELETE FROM agent_run_retrieval_context
        WHERE run_id=NEW.id AND workspace_id=NEW.workspace_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER agent_run_retrieval_context_terminal_cleanup
    AFTER UPDATE OF status ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION clear_terminal_agent_run_retrieval_context();
