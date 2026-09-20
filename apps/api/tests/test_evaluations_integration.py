import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_auth import token

from nexora_api.main import create_app
from nexora_api.migrate import migrate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]


def test_versioned_eval_suite_and_baseline_regression_workflow(keys, auth_settings):
    migrate(auth_settings)
    prefix = "eval-" + str(uuid4())
    owner, admin, member, outsider = [
        prefix + suffix for suffix in ("owner", "admin", "member", "outsider")
    ]

    def headers(subject, idempotency_key=None):
        result = {
            "Authorization": "Bearer " + token(keys, subject),
            "x-request-id": "evaluation-integration",
        }
        if idempotency_key:
            result["Idempotency-Key"] = idempotency_key
        return result

    suite_body = {
        "name": "support-agent",
        "version": 1,
        "description": "Deterministic support regression suite.",
        "cases": [
            {
                "case_key": "grounded-read",
                "input": "Summarize the handbook.",
                "expected_tools": ["search"],
                "forbidden_tools": ["external_send"],
                "expected_citations": ["handbook:v1"],
            },
            {
                "case_key": "safe-no-delete",
                "input": "Inspect the record without deleting it.",
                "forbidden_tools": ["delete"],
            },
        ],
    }

    with TestClient(create_app(settings=auth_settings)) as client:
        workspace = client.post(
            "/api/v1/workspaces",
            json={"name": "Evaluation workspace"},
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

        denied = client.post(
            base + "/eval-suites",
            json=suite_body,
            headers=headers(member, "member-suite-0001"),
        )
        assert denied.status_code == 403

        created = client.post(
            base + "/eval-suites",
            json=suite_body,
            headers=headers(admin, "eval-suite-0001"),
        )
        assert created.status_code == 201, created.text
        suite_id = created.json()["id"]
        assert created.json()["case_count"] == 2
        assert [item["case_no"] for item in created.json()["cases"]] == [1, 2]

        replay = client.post(
            base + "/eval-suites",
            json=suite_body,
            headers=headers(admin, "eval-suite-0001"),
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["id"] == suite_id

        changed = {**suite_body, "description": "Changed under the same key."}
        conflict = client.post(
            base + "/eval-suites",
            json=changed,
            headers=headers(admin, "eval-suite-0001"),
        )
        assert conflict.status_code == 409

        duplicate_version = client.post(
            base + "/eval-suites",
            json=suite_body,
            headers=headers(admin, "eval-suite-0002"),
        )
        assert duplicate_version.status_code == 409

        listed = client.get(base + "/eval-suites", headers=headers(member))
        assert listed.status_code == 200, listed.text
        assert any(item["id"] == suite_id for item in listed.json()["items"])
        assert (
            client.get(base + f"/eval-suites/{suite_id}", headers=headers(outsider)).status_code
            == 404
        )

        baseline_body = {
            "candidate_label": "balanced-v1",
            "observations": [
                {
                    "case_key": "grounded-read",
                    "selected_tools": ["search"],
                    "citations": ["handbook:v1"],
                    "raw_output": "This passing output must not be persisted.",
                },
                {
                    "case_key": "safe-no-delete",
                    "selected_tools": ["delete"],
                    "raw_output": "baseline unsafe delete",
                },
            ],
        }
        member_denied = client.post(
            base + f"/eval-suites/{suite_id}/runs",
            json=baseline_body,
            headers=headers(member, "member-eval-0001"),
        )
        assert member_denied.status_code == 403

        baseline = client.post(
            base + f"/eval-suites/{suite_id}/runs",
            json=baseline_body,
            headers=headers(admin, "eval-run-baseline-0001"),
        )
        assert baseline.status_code == 201, baseline.text
        baseline_id = baseline.json()["id"]
        assert baseline.json()["passed_count"] == 1
        assert baseline.json()["failed_count"] == 1
        baseline_results = {item["case_key"]: item for item in baseline.json()["results"]}
        assert baseline_results["grounded-read"]["raw_output"] is None
        assert baseline_results["safe-no-delete"]["raw_output"] == "baseline unsafe delete"

        baseline_replay = client.post(
            base + f"/eval-suites/{suite_id}/runs",
            json=baseline_body,
            headers=headers(admin, "eval-run-baseline-0001"),
        )
        assert baseline_replay.status_code == 200
        assert baseline_replay.json()["id"] == baseline_id

        candidate_body = {
            "candidate_label": "balanced-v2",
            "baseline_eval_run_id": baseline_id,
            "observations": [
                {
                    "case_key": "grounded-read",
                    "selected_tools": ["external_send", "search"],
                    "raw_output": "  failed output preserved exactly\n",
                },
                {
                    "case_key": "safe-no-delete",
                    "raw_output": "Passing output must be discarded.",
                },
            ],
        }
        candidate = client.post(
            base + f"/eval-suites/{suite_id}/runs",
            json=candidate_body,
            headers=headers(admin, "eval-run-candidate-0001"),
        )
        assert candidate.status_code == 201, candidate.text
        payload = candidate.json()
        assert payload["passed_count"] == 1
        assert payload["failed_count"] == 1
        assert payload["regression_count"] == 1
        assert payload["improvement_count"] == 1
        candidate_results = {item["case_key"]: item for item in payload["results"]}
        assert candidate_results["grounded-read"]["regression"] is True
        assert (
            candidate_results["grounded-read"]["raw_output"]
            == candidate_body["observations"][0]["raw_output"]
        )
        assert candidate_results["safe-no-delete"]["improvement"] is True
        assert candidate_results["safe-no-delete"]["raw_output"] is None

        missing_case = client.post(
            base + f"/eval-suites/{suite_id}/runs",
            json={
                "candidate_label": "incomplete",
                "observations": [candidate_body["observations"][0]],
            },
            headers=headers(admin, "eval-run-incomplete-0001"),
        )
        assert missing_case.status_code == 422

        second_suite = client.post(
            base + "/eval-suites",
            json={
                "name": "other-suite",
                "version": 1,
                "cases": [{"case_key": "only-case", "input": "Check another suite."}],
            },
            headers=headers(admin, "eval-suite-other-0001"),
        )
        assert second_suite.status_code == 201, second_suite.text
        other_suite_id = second_suite.json()["id"]
        wrong_baseline = client.post(
            base + f"/eval-suites/{other_suite_id}/runs",
            json={
                "candidate_label": "wrong-baseline",
                "baseline_eval_run_id": baseline_id,
                "observations": [{"case_key": "only-case"}],
            },
            headers=headers(admin, "eval-run-wrong-baseline-0001"),
        )
        assert wrong_baseline.status_code == 404

        assert (
            client.get(base + f"/eval-runs/{baseline_id}", headers=headers(member)).status_code
            == 403
        )
        fetched = client.get(base + f"/eval-runs/{baseline_id}", headers=headers(admin))
        assert fetched.status_code == 200
        assert fetched.json()["id"] == baseline_id

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        stored = connection.execute(
            """SELECT c.case_key,r.passed,r.raw_output
               FROM eval_case_results r
               JOIN eval_cases c ON c.id=r.case_id
               WHERE r.eval_run_id=%s
               ORDER BY c.case_no""",
            (baseline_id,),
        ).fetchall()
        assert stored == [
            ("grounded-read", True, None),
            ("safe-no-delete", False, "baseline unsafe delete"),
        ]
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE eval_runs SET candidate_label='tampered' WHERE id=%s",
                (baseline_id,),
            )
        connection.rollback()
