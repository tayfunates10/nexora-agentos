import os
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_auth import token

from nexora_api.main import create_app
from nexora_api.migrate import migrate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]


def test_agent_run_eval_import_is_requester_scoped_and_fail_closed(keys, auth_settings):
    migrate(auth_settings)
    prefix = "eval-import-" + str(uuid4())
    owner, admin, member = [prefix + suffix for suffix in ("owner", "admin", "member")]

    def headers(subject, idempotency_key=None):
        result = {
            "Authorization": "Bearer " + token(keys, subject),
            "x-request-id": "agent-run-eval-import",
        }
        if idempotency_key:
            result["Idempotency-Key"] = idempotency_key
        return result

    with TestClient(create_app(settings=auth_settings)) as client:
        workspace = client.post(
            "/api/v1/workspaces",
            json={"name": "Agent run eval import"},
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
                "name": "real-runs",
                "version": 1,
                "cases": [
                    {
                        "case_key": "uses-search",
                        "input": "Find the account record.",
                        "expected_tools": ["search"],
                    },
                    {
                        "case_key": "needs-lookup",
                        "input": "Inspect the account safely.",
                        "expected_tools": ["lookup"],
                        "forbidden_tools": ["delete"],
                    },
                ],
            },
            headers=headers(admin, "agent-run-suite-0001"),
        )
        assert suite.status_code == 201, suite.text
        suite_id = suite.json()["id"]

        agent_id = uuid4()

        def insert_run(subject, input_text, *, selected_tool=None, status="succeeded"):
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
                        "Evaluation agent",
                        "Answer safely.",
                        auth_settings.auth_issuer,
                        admin,
                    ),
                )
                connection.execute(
                    """INSERT INTO agent_runs
                       (id,workspace_id,agent_id,requested_by_issuer,requested_by_subject,
                        input_text,request_hash,idempotency_key,trace_id,status,finished_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               CASE WHEN %s='succeeded' THEN now() ELSE NULL END)""",
                    (
                        run_id,
                        UUID(workspace_id),
                        agent_id,
                        auth_settings.auth_issuer,
                        subject,
                        input_text,
                        "a" * 64,
                        "run-" + str(run_id),
                        uuid4(),
                        status,
                        status,
                    ),
                )
                if status == "succeeded":
                    step_no = 0
                    if selected_tool:
                        connection.execute(
                            """INSERT INTO agent_model_steps
                               (workspace_id,run_id,step_no,provider,model,routing_reason,response)
                               VALUES (%s,%s,%s,'test','model-v1','test',%s)""",
                            (
                                UUID(workspace_id),
                                run_id,
                                step_no,
                                Jsonb(
                                    {
                                        "text": None,
                                        "tool_calls": [
                                            {
                                                "id": "call-1",
                                                "name": selected_tool,
                                                "arguments": {"private": "excluded"},
                                            }
                                        ],
                                        "structured_output": None,
                                        "usage": {"input_tokens": 4, "output_tokens": 1},
                                        "finish_reason": "tool_call",
                                    }
                                ),
                            ),
                        )
                        step_no += 1
                    connection.execute(
                        """INSERT INTO agent_model_steps
                           (workspace_id,run_id,step_no,provider,model,routing_reason,response)
                           VALUES (%s,%s,%s,'test','model-v1','test',%s)""",
                        (
                            UUID(workspace_id),
                            run_id,
                            step_no,
                            Jsonb(
                                {
                                    "text": f"Final answer for {input_text}",
                                    "tool_calls": [],
                                    "structured_output": None,
                                    "usage": {"input_tokens": 5, "output_tokens": 2},
                                    "finish_reason": "stop",
                                }
                            ),
                        ),
                    )
            return str(run_id)

        search_run = insert_run(admin, "Find the account record.", selected_tool="search")
        no_lookup_run = insert_run(admin, "Inspect the account safely.")
        foreign_requester_run = insert_run(
            owner, "Find the account record.", selected_tool="search"
        )
        mismatched_run = insert_run(admin, "Different input.")
        queued_run = insert_run(admin, "Find the account record.", status="queued")

        import_body = {
            "candidate_label": "real-agent-runs-v1",
            "cases": [
                {"case_key": "uses-search", "agent_run_id": search_run},
                {"case_key": "needs-lookup", "agent_run_id": no_lookup_run},
            ],
        }

        denied_member = client.post(
            base + f"/eval-suites/{suite_id}/run-imports",
            json=import_body,
            headers=headers(member, "member-import-0001"),
        )
        assert denied_member.status_code == 403

        imported = client.post(
            base + f"/eval-suites/{suite_id}/run-imports",
            json=import_body,
            headers=headers(admin, "agent-run-import-0001"),
        )
        assert imported.status_code == 201, imported.text
        payload = imported.json()
        assert payload["passed_count"] == 1
        assert payload["failed_count"] == 1
        results = {item["case_key"]: item for item in payload["results"]}
        assert results["uses-search"]["selected_tools"] == ["search"]
        assert results["uses-search"]["source_agent_run_id"] == search_run
        assert results["uses-search"]["raw_output"] is None
        assert results["needs-lookup"]["source_agent_run_id"] == no_lookup_run
        assert results["needs-lookup"]["failures"] == ["missing_tools:lookup"]
        assert results["needs-lookup"]["raw_output"] == (
            "Final answer for Inspect the account safely."
        )

        replay = client.post(
            base + f"/eval-suites/{suite_id}/run-imports",
            json=import_body,
            headers=headers(admin, "agent-run-import-0001"),
        )
        assert replay.status_code == 200
        assert replay.json()["id"] == payload["id"]

        changed_mapping = {
            **import_body,
            "cases": [
                {"case_key": "uses-search", "agent_run_id": mismatched_run},
                {"case_key": "needs-lookup", "agent_run_id": no_lookup_run},
            ],
        }
        conflict = client.post(
            base + f"/eval-suites/{suite_id}/run-imports",
            json=changed_mapping,
            headers=headers(admin, "agent-run-import-0001"),
        )
        assert conflict.status_code == 409

        requester_denied = client.post(
            base + f"/eval-suites/{suite_id}/run-imports",
            json={
                **import_body,
                "cases": [
                    {"case_key": "uses-search", "agent_run_id": foreign_requester_run},
                    {"case_key": "needs-lookup", "agent_run_id": no_lookup_run},
                ],
            },
            headers=headers(admin, "agent-run-import-0002"),
        )
        assert requester_denied.status_code == 404

        mismatch = client.post(
            base + f"/eval-suites/{suite_id}/run-imports",
            json={
                **import_body,
                "cases": [
                    {"case_key": "uses-search", "agent_run_id": mismatched_run},
                    {"case_key": "needs-lookup", "agent_run_id": no_lookup_run},
                ],
            },
            headers=headers(admin, "agent-run-import-0003"),
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["detail"] == "agent_run_input_mismatch"

        unfinished = client.post(
            base + f"/eval-suites/{suite_id}/run-imports",
            json={
                **import_body,
                "cases": [
                    {"case_key": "uses-search", "agent_run_id": queued_run},
                    {"case_key": "needs-lookup", "agent_run_id": no_lookup_run},
                ],
            },
            headers=headers(admin, "agent-run-import-0004"),
        )
        assert unfinished.status_code == 409
        assert unfinished.json()["detail"] == "agent_run_not_evaluable"

        citation_suite = client.post(
            base + "/eval-suites",
            json={
                "name": "citation-suite",
                "version": 1,
                "cases": [
                    {
                        "case_key": "grounded",
                        "input": "Find the account record.",
                        "expected_citations": ["handbook:v1"],
                    }
                ],
            },
            headers=headers(admin, "citation-suite-0001"),
        )
        assert citation_suite.status_code == 201, citation_suite.text
        citation_import = client.post(
            base + f"/eval-suites/{citation_suite.json()['id']}/run-imports",
            json={
                "candidate_label": "no-fake-citations",
                "cases": [{"case_key": "grounded", "agent_run_id": search_run}],
            },
            headers=headers(admin, "citation-import-0001"),
        )
        assert citation_import.status_code == 422
        assert citation_import.json()["detail"] == "verified_retrieval_provenance_required"

    with psycopg.connect(auth_settings.database_url.get_secret_value()) as connection:
        sources = connection.execute(
            """SELECT case_id,agent_run_id
               FROM eval_agent_run_sources
               WHERE eval_run_id=%s
               ORDER BY agent_run_id""",
            (UUID(payload["id"]),),
        ).fetchall()
        assert {str(row[1]) for row in sources} == {search_run, no_lookup_run}
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                """UPDATE eval_agent_run_sources
                   SET agent_run_id=%s WHERE eval_run_id=%s""",
                (uuid4(), UUID(payload["id"])),
            )
        connection.rollback()
