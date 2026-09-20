-- Delivery intent is written with the alert it belongs to, so a notification can
-- never be owed for a crossing that was rolled back, and a crossing can never be
-- committed without its notification being queued.
CREATE TABLE workspace_spend_alert_outbox (
    -- One notification per alert: the primary key is the deduplication.
    alert_id uuid PRIMARY KEY REFERENCES workspace_spend_alerts(id),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text CHECK (lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 100),
    lease_expires_at timestamptz,
    delivered_at timestamptz,
    dead_lettered_at timestamptz,
    last_error text CHECK (last_error IS NULL OR length(last_error) BETWEEN 1 AND 100),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
    -- A notification is delivered or abandoned, never both.
    CHECK (delivered_at IS NULL OR dead_lettered_at IS NULL)
);
CREATE INDEX workspace_spend_alert_outbox_pending
    ON workspace_spend_alert_outbox(available_at, created_at, alert_id)
    WHERE delivered_at IS NULL AND dead_lettered_at IS NULL;

CREATE FUNCTION enforce_spend_alert_outbox_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (
        NEW.alert_id IS DISTINCT FROM OLD.alert_id
        OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    ) THEN
        RAISE EXCEPTION 'spend alert notification identity is immutable';
    END IF;
    -- A delivered notification is terminal: no retry may resend it.
    IF OLD.delivered_at IS NOT NULL AND NEW.delivered_at IS DISTINCT FROM OLD.delivered_at THEN
        RAISE EXCEPTION 'spend alert notification was already delivered';
    END IF;
    IF OLD.dead_lettered_at IS NOT NULL
        AND NEW.dead_lettered_at IS DISTINCT FROM OLD.dead_lettered_at THEN
        RAISE EXCEPTION 'spend alert notification was already abandoned';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER workspace_spend_alert_outbox_update_guard
    BEFORE UPDATE ON workspace_spend_alert_outbox
    FOR EACH ROW EXECUTE FUNCTION enforce_spend_alert_outbox_update();

CREATE TRIGGER workspace_spend_alert_outbox_no_delete
    BEFORE DELETE OR TRUNCATE ON workspace_spend_alert_outbox
    FOR EACH STATEMENT EXECUTE FUNCTION reject_spend_ledger_mutation();
