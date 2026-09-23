ALTER TABLE workspace_answer_cache
    ADD COLUMN scope_key char(64),
    ADD COLUMN normalized_question text,
    ADD COLUMN embedding_model text,
    ADD COLUMN embedding_dimensions integer,
    ADD COLUMN embedding vector;

ALTER TABLE workspace_answer_cache
    ADD CONSTRAINT workspace_answer_cache_scope_key_format
        CHECK (scope_key IS NULL OR scope_key ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT workspace_answer_cache_question_length
        CHECK (
            normalized_question IS NULL
            OR length(normalized_question) BETWEEN 1 AND 12000
        ),
    ADD CONSTRAINT workspace_answer_cache_embedding_model_length
        CHECK (
            embedding_model IS NULL
            OR length(embedding_model) BETWEEN 1 AND 255
        ),
    ADD CONSTRAINT workspace_answer_cache_embedding_dimensions
        CHECK (
            embedding_dimensions IS NULL
            OR embedding_dimensions BETWEEN 1 AND 4096
        ),
    ADD CONSTRAINT workspace_answer_cache_semantic_shape
        CHECK (
            embedding IS NULL
            OR (
                scope_key IS NOT NULL
                AND normalized_question IS NOT NULL
                AND embedding_model IS NOT NULL
                AND embedding_dimensions IS NOT NULL
            )
        );

CREATE INDEX workspace_answer_cache_semantic_scope_idx
    ON workspace_answer_cache
       (workspace_id, agent_id, scope_key, provider, model,
        embedding_model, embedding_dimensions, expires_at)
    WHERE embedding IS NOT NULL;
