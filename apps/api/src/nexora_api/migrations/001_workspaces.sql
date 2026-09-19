CREATE TABLE workspaces (
    id uuid PRIMARY KEY,
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 100),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE workspace_memberships (
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    issuer text NOT NULL CHECK (length(issuer) > 0),
    subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
    role text NOT NULL CHECK (role IN ('owner','admin','member')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, issuer, subject)
);
CREATE INDEX memberships_identity ON workspace_memberships (issuer, subject, workspace_id);
CREATE UNIQUE INDEX one_owner_per_workspace ON workspace_memberships(workspace_id)
    WHERE role='owner';
CREATE TABLE security_events (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    actor_issuer text NOT NULL,
    actor_subject text NOT NULL,
    action text NOT NULL,
    request_id text NOT NULL,
    target_subject text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX security_events_workspace_time ON security_events(workspace_id, created_at, id);
CREATE FUNCTION reject_security_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'security_events is append-only';
END;
$$;
CREATE TRIGGER security_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON security_events
    FOR EACH STATEMENT EXECUTE FUNCTION reject_security_event_mutation();
