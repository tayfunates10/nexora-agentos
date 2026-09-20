import os
from uuid import uuid4

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


def test_evaluation_history_pagination_and_tenant_isolation(keys, auth_settings):
    migrate(auth_settings)
    owner, member, outsider = [str(uuid4()) for _ in range(3)]

    def headers(subject=owner, write=False):
        value = {"Authorization": "Bearer " + token(keys, subject)}
        if write:
            value["Idempotency-Key"] = str(uuid4())
        return value

    with TestClient(create_app(settings=auth_settings)) as client:

        def workspace(subject):
            response = client.post(
                "/api/v1/workspaces", json={"name": "History"}, headers=headers(subject)
            )
            assert response.status_code == 201, response.text
            return "/api/v1/workspaces/" + response.json()["id"]

        base = workspace(owner)
        other = workspace(outsider)

        def suite(path, subject=owner):
            response = client.post(
                path + "/eval-suites",
                json={
                    "name": str(uuid4()),
                    "version": 1,
                    "cases": [{"case_key": "case", "input": "Read", "forbidden_tools": ["delete"]}],
                },
                headers=headers(subject, True),
            )
            assert response.status_code == 201, response.text
            return response.json()["id"]

        suite_id = suite(base)
        other_suite = suite(other, outsider)
        same_workspace_suite = suite(base)
        history = base + f"/eval-suites/{suite_id}/runs"

        def run(path, label, subject=owner):
            response = client.post(
                path,
                json={
                    "candidate_label": label,
                    "observations": [
                        {
                            "case_key": "case",
                            "selected_tools": ["delete"],
                            "raw_output": "private failed evidence",
                        }
                    ],
                },
                headers=headers(subject, True),
            )
            assert response.status_code == 201, response.text
            return response.json()["id"]

        empty = client.get(history, headers=headers())
        assert empty.status_code == 200
        assert empty.json() == {"items": [], "next_cursor": None}
        old = run(history, "old")
        middle = run(history, "middle")
        newest = run(history, "newest")
        foreign_run = run(other + f"/eval-suites/{other_suite}/runs", "foreign", outsider)
        unrelated_run = run(base + f"/eval-suites/{same_workspace_suite}/runs", "other-suite")

        first = client.get(history, params={"limit": 2}, headers=headers())
        assert first.status_code == 200, first.text
        assert [row["id"] for row in first.json()["items"]] == [newest, middle]
        assert first.json()["next_cursor"] == middle
        assert "private failed evidence" not in first.text
        assert all(
            "results" not in row and "raw_output" not in row for row in first.json()["items"]
        )

        run(history, "arrived-between-pages")
        second = client.get(history, params={"limit": 2, "cursor": middle}, headers=headers())
        assert second.status_code == 200, second.text
        assert [row["id"] for row in second.json()["items"]] == [old]
        assert second.json()["next_cursor"] is None

        for cursor in (foreign_run, unrelated_run, str(uuid4())):
            assert (
                client.get(history, params={"cursor": cursor}, headers=headers()).status_code == 404
            )
        for params in ({"limit": 0}, {"limit": 101}, {"cursor": "invalid"}):
            assert client.get(history, params=params, headers=headers()).status_code == 422
        assert client.get(history).status_code == 401
        assert client.get(history, headers=headers(outsider)).status_code == 404
        assert (
            client.get(base + f"/eval-suites/{other_suite}/runs", headers=headers()).status_code
            == 404
        )
        assert (
            client.get(base + f"/eval-suites/{uuid4()}/runs", headers=headers()).status_code == 404
        )

        granted = client.put(
            base + "/members", json={"subject": member, "role": "admin"}, headers=headers()
        )
        assert granted.status_code == 200
        assert client.get(history, headers=headers(member)).status_code == 200
        revoked = client.put(
            base + "/members", json={"subject": member, "role": "member"}, headers=headers()
        )
        assert revoked.status_code == 200
        assert client.get(history, headers=headers(member)).status_code == 403
        assert client.get(base + "/eval-runs/" + newest, headers=headers(member)).status_code == 403
