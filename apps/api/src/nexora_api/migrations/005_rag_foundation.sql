CREATE TABLE rag_sources (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    source_key text NOT NULL CHECK (length(source_key) BETWEEN 1 AND 255),
    version text NOT NULL CHECK (length(version) BETWEEN 1 AND 128),
    title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 500),
    content_hash text NOT NULL CHECK (length(content_hash) = 64),
    access_scope text NOT NULL CHECK (access_scope IN ('workspace','restricted')),
    is_current boolean NOT NULL DEFAULT true,
    created_by_issuer text NOT NULL,
    created_by_subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, workspace_id),
    UNIQUE (workspace_id, source_key, version)
);
CREATE UNIQUE INDEX rag_sources_one_current_version
    ON rag_sources(workspace_id, source_key)
    WHERE is_current;
CREATE INDEX rag_sources_workspace_current
    ON rag_sources(workspace_id, is_current, source_key);

CREATE TABLE rag_source_acl (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    source_id uuid NOT NULL,
    issuer text NOT NULL CHECK (length(issuer) BETWEEN 1 AND 500),
    subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, issuer, subject),
    FOREIGN KEY (source_id, workspace_id)
        REFERENCES rag_sources(id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX rag_source_acl_identity
    ON rag_source_acl(workspace_id, issuer, subject, source_id);

CREATE TABLE rag_chunks (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    source_id uuid NOT NULL,
    source_version text NOT NULL CHECK (length(source_version) BETWEEN 1 AND 128),
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    start_offset integer NOT NULL CHECK (start_offset >= 0),
    end_offset integer NOT NULL CHECK (end_offset > start_offset),
    content text NOT NULL CHECK (length(content) BETWEEN 1 AND 12000),
    content_hash text NOT NULL CHECK (length(content_hash) = 64),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    access_scope text NOT NULL CHECK (access_scope IN ('workspace','restricted')),
    embedding_model text NOT NULL CHECK (length(embedding_model) BETWEEN 1 AND 255),
    embedding_dimensions integer NOT NULL CHECK (embedding_dimensions BETWEEN 1 AND 4096),
    embedding vector NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, chunk_index),
    FOREIGN KEY (source_id, workspace_id)
        REFERENCES rag_sources(id, workspace_id) ON DELETE CASCADE,
    CHECK (vector_dims(embedding) = embedding_dimensions)
);
CREATE INDEX rag_chunks_retrieval_filter
    ON rag_chunks(workspace_id, embedding_model, embedding_dimensions, access_scope);
CREATE INDEX rag_chunks_source_order
    ON rag_chunks(source_id, chunk_index);
