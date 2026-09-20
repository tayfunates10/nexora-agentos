-- Thresholds are part of the budget decision an owner or admin records, so they
-- travel with the budget row and with its append-only history.
ALTER TABLE workspace_spend_budgets
    ADD COLUMN alert_thresholds smallint[] NOT NULL DEFAULT '{}'::smallint[];
ALTER TABLE workspace_spend_budgets
    ADD CONSTRAINT workspace_spend_budgets_thresholds_bounded CHECK (
        cardinality(alert_thresholds) <= 5
        AND 1 <= ALL (alert_thresholds)
        AND 100 >= ALL (alert_thresholds)
    );

ALTER TABLE workspace_spend_budget_events
    ADD COLUMN alert_thresholds smallint[] NOT NULL DEFAULT '{}'::smallint[];
ALTER TABLE workspace_spend_budget_events
    ADD CONSTRAINT workspace_spend_budget_events_thresholds_bounded CHECK (
        cardinality(alert_thresholds) <= 5
        AND 1 <= ALL (alert_thresholds)
        AND 100 >= ALL (alert_thresholds)
    );

CREATE TABLE workspace_spend_alerts (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    -- First day of the UTC accounting month the alert belongs to.
    period_start date NOT NULL CHECK (date_trunc('month', period_start) = period_start),
    threshold_percent smallint NOT NULL CHECK (threshold_percent BETWEEN 1 AND 100),
    -- The budget as it stood when the threshold was crossed; a later budget change
    -- must not rewrite what was alerted on.
    monthly_limit_micros bigint NOT NULL CHECK (monthly_limit_micros >= 0),
    consumed_micros bigint NOT NULL CHECK (consumed_micros >= 0),
    enforcement text NOT NULL CHECK (enforcement IN ('enforce','monitor')),
    created_at timestamptz NOT NULL DEFAULT now(),
    -- One alert per threshold per workspace per period: the database, not the
    -- worker, is what keeps a busy month from repeating the same warning.
    UNIQUE (workspace_id, period_start, threshold_percent)
);
CREATE INDEX workspace_spend_alerts_period
    ON workspace_spend_alerts(workspace_id, period_start DESC, threshold_percent DESC);

CREATE TRIGGER workspace_spend_alerts_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON workspace_spend_alerts
    FOR EACH STATEMENT EXECUTE FUNCTION reject_spend_ledger_mutation();
