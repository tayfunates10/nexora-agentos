CREATE TABLE eval_suites (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 100),
    version integer NOT NULL CHECK (version > 0),
    description text CHECK (
        description IS NULL OR length(btrim(description)) BETWEEN 1 AND 2000
    ),
    created_by_issuer text NOT NULL,
    created_by_subject text NOT NULL,
    request_hash text NOT NULL CHECK (length(request_hash) = 64),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, workspace_id),
    UNIQUE (workspace_id, name, version),
    UNIQUE (workspace_id, created_by_issuer, created_by_subject, idempotency_key)
);
CREATE INDEX eval_suites_workspace_version
    ON eval_suites(workspace_id, name, version DESC, id);

CREATE TABLE eval_cases (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    suite_id uuid NOT NULL,
    case_no integer NOT NULL CHECK (case_no > 0),
    case_key text NOT NULL CHECK (length(btrim(case_key)) BETWEEN 1 AND 100),
    input_text text NOT NULL CHECK (length(btrim(input_text)) BETWEEN 1 AND 20000),
    expected_tools text[] NOT NULL DEFAULT '{}' CHECK (cardinality(expected_tools) <= 32),
    forbidden_tools text[] NOT NULL DEFAULT '{}' CHECK (cardinality(forbidden_tools) <= 32),
    expected_citations text[] NOT NULL DEFAULT '{}' CHECK (cardinality(expected_citations) <= 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, workspace_id),
    UNIQUE (suite_id, case_no),
    UNIQUE (suite_id, case_key),
    FOREIGN KEY (suite_id, workspace_id) REFERENCES eval_suites(id, workspace_id)
);
CREATE INDEX eval_cases_suite ON eval_cases(suite_id, case_no);

CREATE TABLE eval_runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    suite_id uuid NOT NULL,
    candidate_label text NOT NULL CHECK (length(btrim(candidate_label)) BETWEEN 1 AND 128),
    baseline_eval_run_id uuid,
    created_by_issuer text NOT NULL,
    created_by_subject text NOT NULL,
    request_hash text NOT NULL CHECK (length(request_hash) = 64),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    case_count integer NOT NULL CHECK (case_count > 0),
    passed_count integer NOT NULL CHECK (passed_count >= 0),
    failed_count integer NOT NULL CHECK (failed_count >= 0),
    regression_count integer NOT NULL DEFAULT 0 CHECK (regression_count >= 0),
    improvement_count integer NOT NULL DEFAULT 0 CHECK (improvement_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, workspace_id),
    UNIQUE (workspace_id, created_by_issuer, created_by_subject, idempotency_key),
    FOREIGN KEY (suite_id, workspace_id) REFERENCES eval_suites(id, workspace_id),
    FOREIGN KEY (baseline_eval_run_id, workspace_id) REFERENCES eval_runs(id, workspace_id),
    CHECK (passed_count + failed_count = case_count),
    CHECK (regression_count <= case_count),
    CHECK (improvement_count <= case_count)
);
CREATE INDEX eval_runs_suite_time ON eval_runs(suite_id, created_at DESC, id);

CREATE TABLE eval_case_results (
    eval_run_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    case_id uuid NOT NULL,
    passed boolean NOT NULL,
    failures jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(failures) = 'array'),
    selected_tools text[] NOT NULL DEFAULT '{}' CHECK (cardinality(selected_tools) <= 32),
    citations text[] NOT NULL DEFAULT '{}' CHECK (cardinality(citations) <= 64),
    raw_output text CHECK (raw_output IS NULL OR length(raw_output) <= 50000),
    baseline_passed boolean,
    regression boolean NOT NULL DEFAULT false,
    improvement boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (eval_run_id, case_id),
    FOREIGN KEY (eval_run_id, workspace_id) REFERENCES eval_runs(id, workspace_id),
    FOREIGN KEY (case_id, workspace_id) REFERENCES eval_cases(id, workspace_id),
    CHECK (NOT passed OR raw_output IS NULL),
    CHECK (NOT (regression AND improvement))
);
CREATE INDEX eval_case_results_case ON eval_case_results(case_id, created_at DESC);

CREATE FUNCTION reject_evaluation_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'evaluation records are append-only';
END;
$$;

CREATE TRIGGER eval_suites_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON eval_suites
    FOR EACH STATEMENT EXECUTE FUNCTION reject_evaluation_mutation();
CREATE TRIGGER eval_cases_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON eval_cases
    FOR EACH STATEMENT EXECUTE FUNCTION reject_evaluation_mutation();
CREATE TRIGGER eval_runs_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON eval_runs
    FOR EACH STATEMENT EXECUTE FUNCTION reject_evaluation_mutation();
CREATE TRIGGER eval_case_results_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON eval_case_results
    FOR EACH STATEMENT EXECUTE FUNCTION reject_evaluation_mutation();
