CREATE TABLE knowledge_ingestion_jobs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    source_key text NOT NULL CHECK (length(source_key) BETWEEN 1 AND 255),
    version text NOT NULL CHECK (length(version) BETWEEN 1 AND 128),
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 500),
    text_content text NOT NULL CHECK (length(text_content) BETWEEN 1 AND 500000),
    access_scope text NOT NULL CHECK (access_scope IN ('workspace','restricted')),
    acl jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(acl) = 'array'),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    max_chars integer NOT NULL CHECK (max_chars BETWEEN 200 AND 4000),
    overlap_chars integer NOT NULL CHECK (
        overlap_chars >= 0 AND overlap_chars < max_chars
    ),
    request_hash text NOT NULL CHECK (length(request_hash) = 64),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    requested_by_issuer text NOT NULL,
    requested_by_subject text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued','running','succeeded','failed','cancelled')
    ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    source_id uuid,
    chunk_count integer CHECK (chunk_count IS NULL OR chunk_count >= 0),
    embedding_input_tokens integer CHECK (
        embedding_input_tokens IS NULL OR embedding_input_tokens >= 0
    ),
    error_code text CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 100),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (
        workspace_id, requested_by_issuer, requested_by_subject, idempotency_key
    ),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);
CREATE INDEX knowledge_ingestion_pending
    ON knowledge_ingestion_jobs(status, available_at, created_at, id)
    WHERE status IN ('queued','running');
CREATE INDEX knowledge_ingestion_workspace_time
    ON knowledge_ingestion_jobs(workspace_id, created_at DESC, id);

CREATE FUNCTION enforce_knowledge_ingestion_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'queued' AND NEW.status IN ('running','cancelled') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'running' AND NEW.status IN ('queued','succeeded','failed','cancelled') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'illegal knowledge ingestion transition: % -> %', OLD.status, NEW.status;
END;
$$;
CREATE TRIGGER knowledge_ingestion_transition
    BEFORE UPDATE OF status ON knowledge_ingestion_jobs
    FOR EACH ROW EXECUTE FUNCTION enforce_knowledge_ingestion_transition();
