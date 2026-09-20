CREATE TABLE eval_judge_runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    eval_run_id uuid NOT NULL,
    requested_by_issuer text NOT NULL,
    requested_by_subject text NOT NULL,
    request_hash text NOT NULL CHECK (length(request_hash) = 64),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    case_count integer NOT NULL CHECK (case_count > 0),
    status text NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued','running','succeeded','failed')
    ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    judge_provider text,
    judge_model text,
    prompt_version text,
    error_code text CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 100),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE (id, workspace_id),
    UNIQUE (id, workspace_id, eval_run_id),
    UNIQUE (
        workspace_id, requested_by_issuer, requested_by_subject, idempotency_key
    ),
    FOREIGN KEY (eval_run_id, workspace_id) REFERENCES eval_runs(id, workspace_id),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
    CHECK (
        (judge_provider IS NULL AND judge_model IS NULL AND prompt_version IS NULL)
        OR
        (judge_provider IS NOT NULL AND judge_model IS NOT NULL AND prompt_version IS NOT NULL)
    )
);
CREATE INDEX eval_judge_runs_pending
    ON eval_judge_runs(status, available_at, created_at, id)
    WHERE status IN ('queued','running');
CREATE INDEX eval_judge_runs_eval
    ON eval_judge_runs(workspace_id, eval_run_id, created_at DESC, id);

CREATE TABLE eval_judge_case_scores (
    judge_run_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    eval_run_id uuid NOT NULL,
    case_id uuid NOT NULL,
    task_completion smallint NOT NULL CHECK (task_completion BETWEEN 0 AND 4),
    answer_relevance smallint NOT NULL CHECK (answer_relevance BETWEEN 0 AND 4),
    clarity smallint NOT NULL CHECK (clarity BETWEEN 0 AND 4),
    quality_milli integer GENERATED ALWAYS AS (
        ((task_completion + answer_relevance + clarity) * 1000 + 6) / 12
    ) STORED,
    rationale text NOT NULL CHECK (length(btrim(rationale)) BETWEEN 1 AND 1000),
    baseline_task_completion smallint CHECK (
        baseline_task_completion IS NULL OR baseline_task_completion BETWEEN 0 AND 4
    ),
    baseline_answer_relevance smallint CHECK (
        baseline_answer_relevance IS NULL OR baseline_answer_relevance BETWEEN 0 AND 4
    ),
    baseline_clarity smallint CHECK (
        baseline_clarity IS NULL OR baseline_clarity BETWEEN 0 AND 4
    ),
    baseline_quality_milli integer GENERATED ALWAYS AS (
        CASE
            WHEN baseline_task_completion IS NULL THEN NULL
            ELSE (
                (
                    baseline_task_completion
                    + baseline_answer_relevance
                    + baseline_clarity
                ) * 1000 + 6
            ) / 12
        END
    ) STORED,
    quality_delta_milli integer GENERATED ALWAYS AS (
        CASE
            WHEN baseline_task_completion IS NULL THEN NULL
            ELSE (
                ((task_completion + answer_relevance + clarity) * 1000 + 6) / 12
                -
                (
                    (
                        baseline_task_completion
                        + baseline_answer_relevance
                        + baseline_clarity
                    ) * 1000 + 6
                ) / 12
            )
        END
    ) STORED,
    input_tokens integer NOT NULL CHECK (input_tokens >= 0),
    output_tokens integer NOT NULL CHECK (output_tokens >= 0),
    latency_ms integer NOT NULL CHECK (latency_ms >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (judge_run_id, case_id),
    FOREIGN KEY (judge_run_id, workspace_id, eval_run_id)
        REFERENCES eval_judge_runs(id, workspace_id, eval_run_id),
    FOREIGN KEY (eval_run_id, case_id)
        REFERENCES eval_case_results(eval_run_id, case_id),
    CHECK (
        (
            baseline_task_completion IS NULL
            AND baseline_answer_relevance IS NULL
            AND baseline_clarity IS NULL
        )
        OR
        (
            baseline_task_completion IS NOT NULL
            AND baseline_answer_relevance IS NOT NULL
            AND baseline_clarity IS NOT NULL
        )
    )
);
CREATE INDEX eval_judge_case_scores_eval
    ON eval_judge_case_scores(workspace_id, eval_run_id, case_id);

CREATE FUNCTION enforce_eval_judge_run_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (
        NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.eval_run_id IS DISTINCT FROM OLD.eval_run_id
        OR NEW.requested_by_issuer IS DISTINCT FROM OLD.requested_by_issuer
        OR NEW.requested_by_subject IS DISTINCT FROM OLD.requested_by_subject
        OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
        OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
        OR NEW.case_count IS DISTINCT FROM OLD.case_count
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    ) THEN
        RAISE EXCEPTION 'evaluation judge request identity is immutable';
    END IF;

    IF OLD.judge_provider IS NOT NULL AND (
        NEW.judge_provider IS DISTINCT FROM OLD.judge_provider
        OR NEW.judge_model IS DISTINCT FROM OLD.judge_model
        OR NEW.prompt_version IS DISTINCT FROM OLD.prompt_version
    ) THEN
        RAISE EXCEPTION 'evaluation judge configuration is immutable once pinned';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status = 'queued' AND NEW.status = 'running' THEN
            RETURN NEW;
        END IF;
        IF OLD.status = 'running' AND NEW.status IN ('queued','succeeded','failed') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'illegal evaluation judge transition: % -> %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER eval_judge_run_update_guard
    BEFORE UPDATE ON eval_judge_runs
    FOR EACH ROW EXECUTE FUNCTION enforce_eval_judge_run_update();

CREATE TRIGGER eval_judge_runs_no_delete
    BEFORE DELETE OR TRUNCATE ON eval_judge_runs
    FOR EACH STATEMENT EXECUTE FUNCTION reject_evaluation_mutation();

CREATE TRIGGER eval_judge_case_scores_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON eval_judge_case_scores
    FOR EACH STATEMENT EXECUTE FUNCTION reject_evaluation_mutation();
