import asyncio
import json
import os
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_auth import token

from nexora_api import metrics
from nexora_api.evaluation_judge_worker import EvaluationJudgeWorker
from nexora_api.main import create_app
from nexora_api.migrate import migrate
from nexora_api.model_routing import ModelPricing, ProviderResponse, ProviderUsage
from nexora_api.runtime_config import EvaluationJudgeConfig

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1",
        reason="Requires PostgreSQL and Redis",
    ),
]


class StubJudgeAdapter:
    name = "test"

    def __init__(self):
        self.calls = []

    async def generate(self, *, model, request, timeout_seconds):
        payload = json.loads(request.messages[-1].content)
        answer = payload["candidate_answer"]
        self.calls.append((model, payload, request.response_schema, timeout_seconds))
        score = 4 if "excellent" in answer else 1
        structured = {
            "task_completion": score,
            "answer_relevance": score,
            "clarity": score,
            "rationale": "Concise quality assessment.",
        }
        return ProviderResponse(
            text=json.dumps(structured),
            tool_calls=(),
            structured_output=structured,
            usage=ProviderUsage(input_tokens=10, output_tokens=5),
            finish_reason="stop",
        )

    async def cancel(self, request_id):
        return None


def test_imported_eval_run_can_be_judged_against_same_model_baseline(keys, auth_settings):
    migrate(auth_settings)
    prefix = "judge-" + str(uuid4())
    owner, admin, member = [prefix + suffix for suffix in ("owner", "admin", "member")]

    def headers(subject, idempotency_key=None):
        value = {
            "Authorization": "Bearer " + token(keys, subject),
            "x-request-id": "evaluation-judge-integration",
        }
        if idempotency_key:
            value["Idempotency-Key"] = idempotency_key
        return value

    with TestClient(create_app(settings=auth_settings)) as client:
        workspace = client.post(
            "/api/v1/workspaces",
            json={"name": "Judge workspace"},
            headers=headers(owner),
        )
        assert workspace.status_code == 201, workspace.text
        workspace_id = workspace.json()["id"]
        base = f"/api/v1/workspaces/{workspace_id}"
        for subject, role in ((admin, "admin"), (member, "member")):
            granted = client.put(
                base + "/members",
                json={"subject": subject, "role": role},
                headers=headers(owner),
            )
            assert granted.status_code == 200, granted.text

        suite = client.post(
            base + "/eval-suites",
            json={
                "name": "judge-suite",
                "version": 1,
                "cases": [
                    {"case_key": "case-one", "input": "Answer task one."},
                    {"case_key": "case-two", "input": "Answer task two."},
                ],
            },
            headers=headers(admin, "judge-suite-0001"),
        )
        assert suite.status_code == 201, suite.text
        suite_id = suite.json()["id"]
        agent_id = uuid4()

        def insert_run(input_text, output_text):
            run_id = uuid4()
            with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
                connection.execute(
                    """INSERT INTO agent_definitions
                       (id,workspace_id,name,instructions,model_profile,
                        created_by_issuer,created_by_subject)
                       VALUES (%s,%s,%s,%s,'default',%s,%s)
                       ON CONFLICT (id) DO NOTHING""",
                    (
                        agent_id,
                        UUID(workspace_id),
                        "Judge source agent",
                        "Answer the task.",
                        auth_settings.auth_issuer,
                        admin,
                    ),
                )
                connection.execute(
                    """INSERT INTO agent_runs
                       (id,workspace_id,agent_id,requested_by_issuer,requested_by_subject,
                        input_text,request_hash,idempotency_key,trace_id,status,finished_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'succeeded',now())""",
                    (
                        run_id,
                        UUID(workspace_id),
                        agent_id,
                        auth_settings.auth_issuer,
                        admin,
                        input_text,
                        "b" * 64,
                        "judge-run-" + str(run_id),
                        uuid4(),
                    ),
                )
                connection.execute(
                    """INSERT INTO agent_model_steps
                       (workspace_id,run_id,step_no,provider,model,routing_reason,response)
                       VALUES (%s,%s,0,'test','source-model','test',%s)""",
                    (
                        UUID(workspace_id),
                        run_id,
                        Jsonb(
                            {
                                "text": output_text,
                                "tool_calls": [],
                                "structured_output": None,
                                "usage": {"input_tokens": 5, "output_tokens": 3},
                                "finish_reason": "stop",
                            }
                        ),
                    ),
                )
            return str(run_id)

        baseline_runs = [
            insert_run("Answer task one.", "poor baseline answer one"),
            insert_run("Answer task two.", "poor baseline answer two"),
        ]
        candidate_runs = [
            insert_run("Answer task one.", "excellent candidate answer one"),
            insert_run("Answer task two.", "excellent candidate answer two"),
        ]

        baseline = client.post(
            base + f"/eval-suites/{suite_id}/run-imports",
            json={
                "candidate_label": "baseline",
                "cases": [
                    {"case_key": "case-one", "agent_run_id": baseline_runs[0]},
                    {"case_key": "case-two", "agent_run_id": baseline_runs[1]},
                ],
            },
            headers=headers(admin, "judge-baseline-import"),
        )
        assert baseline.status_code == 201, baseline.text
        baseline_id = baseline.json()["id"]

        candidate = client.post(
            base + f"/eval-suites/{suite_id}/run-imports",
            json={
                "candidate_label": "candidate",
                "baseline_eval_run_id": baseline_id,
                "cases": [
                    {"case_key": "case-one", "agent_run_id": candidate_runs[0]},
                    {"case_key": "case-two", "agent_run_id": candidate_runs[1]},
                ],
            },
            headers=headers(admin, "judge-candidate-import"),
        )
        assert candidate.status_code == 201, candidate.text
        candidate_id = candidate.json()["id"]

        none_yet = client.get(
            base + f"/eval-runs/{candidate_id}/judge-runs/latest",
            headers=headers(admin),
        )
        assert none_yet.status_code == 404

        denied = client.post(
            base + f"/eval-runs/{candidate_id}/judge-runs",
            headers=headers(member, "judge-member-denied"),
        )
        assert denied.status_code == 403

        queued = client.post(
            base + f"/eval-runs/{candidate_id}/judge-runs",
            headers=headers(admin, "judge-job-0001"),
        )
        assert queued.status_code == 202, queued.text
        judge_run_id = queued.json()["id"]
        assert queued.json()["status"] == "queued"
        assert queued.json()["judge_model"] is None

        latest = client.get(
            base + f"/eval-runs/{candidate_id}/judge-runs/latest",
            headers=headers(admin),
        )
        assert latest.status_code == 200
        assert latest.json()["id"] == judge_run_id
        assert (
            client.get(
                base + f"/eval-runs/{candidate_id}/judge-runs/latest",
                headers=headers(owner),
            ).status_code
            == 404
        )

        replay = client.post(
            base + f"/eval-runs/{candidate_id}/judge-runs",
            headers=headers(admin, "judge-job-0001"),
        )
        assert replay.status_code == 200
        assert replay.json()["id"] == judge_run_id

        assert (
            client.get(
                base + f"/eval-judge-runs/{judge_run_id}",
                headers=headers(owner),
            ).status_code
            == 404
        )

        judge_calls_before = (
            metrics.REGISTRY.get_sample_value(
                "nexora_evaluation_judge_calls_total",
                {"provider": "test", "target": "candidate", "outcome": "success"},
            )
            or 0.0
        )
        judge_jobs_before = (
            metrics.REGISTRY.get_sample_value(
                "nexora_evaluation_judge_jobs_total",
                {"outcome": "succeeded"},
            )
            or 0.0
        )
        model_calls_before = (
            metrics.REGISTRY.get_sample_value(
                "nexora_model_calls_total",
                {"provider": "test", "outcome": "success"},
            )
            or 0.0
        )
        model_output_before = (
            metrics.REGISTRY.get_sample_value(
                "nexora_model_tokens_total",
                {"provider": "test", "kind": "output"},
            )
            or 0.0
        )

        adapter = StubJudgeAdapter()
        worker = EvaluationJudgeWorker(
            auth_settings,
            adapter,
            EvaluationJudgeConfig(
                provider="test",
                model="judge-model-v1",
                allowed_workspaces=[UUID(workspace_id)],
                timeout_seconds=5,
                max_output_tokens=256,
                max_input_chars=10000,
            ),
            worker_id="judge-worker",
            pricing=ModelPricing("judge-pricing-v1", 1000000, 2000000),
            lease_seconds=6,
        )
        assert asyncio.run(worker.process_once()) is True
        partial = client.get(
            base + f"/eval-judge-runs/{judge_run_id}",
            headers=headers(admin),
        )
        assert partial.status_code == 200
        assert partial.json()["scored_count"] == 1
        assert partial.json()["status"] == "queued"

        assert asyncio.run(worker.process_once()) is True
        completed = client.get(
            base + f"/eval-judge-runs/{judge_run_id}",
            headers=headers(admin),
        )
        assert completed.status_code == 200, completed.text
        payload = completed.json()
        assert payload["status"] == "succeeded"
        assert payload["scored_count"] == payload["case_count"] == 2
        assert payload["judge_provider"] == "test"
        assert payload["judge_model"] == "judge-model-v1"
        assert payload["prompt_version"] == "nexora-eval-judge-v1"
        assert payload["quality_milli"] == 1000
        assert payload["baseline_quality_milli"] == 250
        assert payload["quality_delta_milli"] == 750
        assert payload["regression_count"] == 0
        assert payload["improvement_count"] == 2
        assert payload["input_tokens"] == 40
        assert payload["output_tokens"] == 20
        assert payload["model_cost_usd_picos"] == "80000000"
        assert payload["model_cost_call_count"] == 4
        assert payload["model_cost_pricing_complete"] is True
        assert payload["model_cost_pricing_versions"] == ["judge-pricing-v1"]
        assert len(adapter.calls) == 4
        assert all(call[2]["additionalProperties"] is False for call in adapter.calls)
        assert all(result["quality_delta_milli"] == 750 for result in payload["results"])
        assert (
            metrics.REGISTRY.get_sample_value(
                "nexora_evaluation_judge_calls_total",
                {"provider": "test", "target": "candidate", "outcome": "success"},
            )
            == judge_calls_before + 2
        )
        assert (
            metrics.REGISTRY.get_sample_value(
                "nexora_evaluation_judge_jobs_total",
                {"outcome": "succeeded"},
            )
            == judge_jobs_before + 1
        )
        assert (
            metrics.REGISTRY.get_sample_value(
                "nexora_model_calls_total",
                {"provider": "test", "outcome": "success"},
            )
            == model_calls_before + 4
        )
        assert (
            metrics.REGISTRY.get_sample_value(
                "nexora_model_tokens_total",
                {"provider": "test", "kind": "output"},
            )
            == model_output_before + 20
        )

        latest_completed = client.get(
            base + f"/eval-runs/{candidate_id}/judge-runs/latest",
            headers=headers(admin),
        )
        assert latest_completed.status_code == 200
        assert latest_completed.json()["id"] == judge_run_id
        assert latest_completed.json()["status"] == "succeeded"

        deterministic = client.get(
            base + f"/eval-runs/{candidate_id}",
            headers=headers(admin),
        )
        assert deterministic.status_code == 200
        assert deterministic.json()["passed_count"] == 2

        expired = client.post(
            base + f"/eval-runs/{candidate_id}/judge-runs",
            headers=headers(admin, "judge-job-expired"),
        )
        assert expired.status_code == 202
        expired_id = expired.json()["id"]
        with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
            connection.execute(
                """UPDATE eval_judge_runs
                   SET status='running',attempt_count=2,lease_owner='dead-worker',
                       lease_expires_at=now()-interval '1 second',
                       judge_provider='test',judge_model='judge-model-v1',
                       prompt_version='nexora-eval-judge-v1'
                   WHERE id=%s""",
                (expired_id,),
            )

        failed_before = (
            metrics.REGISTRY.get_sample_value(
                "nexora_evaluation_judge_jobs_total",
                {"outcome": "failed"},
            )
            or 0.0
        )
        calls_before = len(adapter.calls)
        assert asyncio.run(worker.process_once()) is False
        assert len(adapter.calls) == calls_before
        assert (
            metrics.REGISTRY.get_sample_value(
                "nexora_evaluation_judge_jobs_total",
                {"outcome": "failed"},
            )
            == failed_before + 1
        )
        with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
            expired_row = connection.execute(
                "SELECT status,error_code,attempt_count FROM eval_judge_runs WHERE id=%s",
                (expired_id,),
            ).fetchone()
        assert expired_row == ("failed", "lease_expired", 3)

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                """UPDATE eval_judge_case_scores
                   SET rationale='tampered' WHERE judge_run_id=%s""",
                (judge_run_id,),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                """UPDATE model_usage_costs
                   SET pricing_version='tampered' WHERE judge_run_id=%s""",
                (judge_run_id,),
            )
        connection.rollback()


def test_judge_worker_rechecks_permission_before_provider_call(keys, auth_settings):
    migrate(auth_settings)
    prefix = "judge-revoked-" + str(uuid4())
    owner, admin = prefix + "owner", prefix + "admin"

    def headers(subject, idempotency_key=None):
        value = {"Authorization": "Bearer " + token(keys, subject)}
        if idempotency_key:
            value["Idempotency-Key"] = idempotency_key
        return value

    with TestClient(create_app(settings=auth_settings)) as client:
        workspace_id = client.post(
            "/api/v1/workspaces",
            json={"name": "Revoked judge workspace"},
            headers=headers(owner),
        ).json()["id"]
        base = f"/api/v1/workspaces/{workspace_id}"
        assert (
            client.put(
                base + "/members",
                json={"subject": admin, "role": "admin"},
                headers=headers(owner),
            ).status_code
            == 200
        )
        suite_id = client.post(
            base + "/eval-suites",
            json={
                "name": "revoked-judge-suite",
                "version": 1,
                "cases": [{"case_key": "only", "input": "Do the task."}],
            },
            headers=headers(admin, "revoked-judge-suite"),
        ).json()["id"]

        agent_id, run_id = uuid4(), uuid4()
        with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
            connection.execute(
                """INSERT INTO agent_definitions
                   (id,workspace_id,name,instructions,model_profile,
                    created_by_issuer,created_by_subject)
                   VALUES (%s,%s,'Judge revoked agent','Answer.','default',%s,%s)""",
                (agent_id, UUID(workspace_id), auth_settings.auth_issuer, admin),
            )
            connection.execute(
                """INSERT INTO agent_runs
                   (id,workspace_id,agent_id,requested_by_issuer,requested_by_subject,
                    input_text,request_hash,idempotency_key,trace_id,status,finished_at)
                   VALUES (%s,%s,%s,%s,%s,'Do the task.',%s,%s,%s,'succeeded',now())""",
                (
                    run_id,
                    UUID(workspace_id),
                    agent_id,
                    auth_settings.auth_issuer,
                    admin,
                    "c" * 64,
                    "revoked-source-run",
                    uuid4(),
                ),
            )
            connection.execute(
                """INSERT INTO agent_model_steps
                   (workspace_id,run_id,step_no,provider,model,routing_reason,response)
                   VALUES (%s,%s,0,'test','source-model','test',%s)""",
                (
                    UUID(workspace_id),
                    run_id,
                    Jsonb(
                        {
                            "text": "excellent candidate answer",
                            "tool_calls": [],
                            "structured_output": None,
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                            "finish_reason": "stop",
                        }
                    ),
                ),
            )

        eval_run = client.post(
            base + f"/eval-suites/{suite_id}/run-imports",
            json={
                "candidate_label": "candidate",
                "cases": [{"case_key": "only", "agent_run_id": str(run_id)}],
            },
            headers=headers(admin, "revoked-judge-import"),
        )
        assert eval_run.status_code == 201, eval_run.text
        judge = client.post(
            base + f"/eval-runs/{eval_run.json()['id']}/judge-runs",
            headers=headers(admin, "revoked-judge-job"),
        )
        assert judge.status_code == 202
        judge_run_id = judge.json()["id"]

        assert (
            client.put(
                base + "/members",
                json={"subject": admin, "role": "member"},
                headers=headers(owner),
            ).status_code
            == 200
        )

        failed_jobs_before = (
            metrics.REGISTRY.get_sample_value(
                "nexora_evaluation_judge_jobs_total",
                {"outcome": "failed"},
            )
            or 0.0
        )
        judge_calls_before = (
            metrics.REGISTRY.get_sample_value(
                "nexora_evaluation_judge_calls_total",
                {"provider": "test", "target": "candidate", "outcome": "success"},
            )
            or 0.0
        )

        adapter = StubJudgeAdapter()
        worker = EvaluationJudgeWorker(
            auth_settings,
            adapter,
            EvaluationJudgeConfig(
                provider="test",
                model="judge-model-v1",
                allowed_workspaces=[UUID(workspace_id)],
            ),
            worker_id="revoked-judge-worker",
            lease_seconds=6,
        )
        assert asyncio.run(worker.process_once()) is True
        assert adapter.calls == []
        assert (
            metrics.REGISTRY.get_sample_value(
                "nexora_evaluation_judge_jobs_total",
                {"outcome": "failed"},
            )
            == failed_jobs_before + 1
        )
        assert (
            metrics.REGISTRY.get_sample_value(
                "nexora_evaluation_judge_calls_total",
                {"provider": "test", "target": "candidate", "outcome": "success"},
            )
            or 0.0
        ) == judge_calls_before

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        row = connection.execute(
            "SELECT status,error_code FROM eval_judge_runs WHERE id=%s",
            (judge_run_id,),
        ).fetchone()
    assert row == ("failed", "judge_permission_revoked")
