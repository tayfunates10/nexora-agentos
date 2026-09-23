CREATE TABLE workspace_answer_cache (
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    agent_id uuid NOT NULL,
    cache_key char(64) NOT NULL CHECK (cache_key ~ '^[0-9a-f]{64}$'),
    provider text NOT NULL,
    model text NOT NULL,
    response jsonb NOT NULL CHECK (jsonb_typeof(response) = 'object'),
    hit_count bigint NOT NULL DEFAULT 0 CHECK (hit_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (workspace_id, agent_id, cache_key),
    CHECK (expires_at > created_at)
);

CREATE INDEX workspace_answer_cache_expiry_idx
    ON workspace_answer_cache (workspace_id, expires_at);
