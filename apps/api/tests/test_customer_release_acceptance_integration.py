import asyncio
import json
import os
from uuid import UUID

import psycopg
import pytest
from catalog_support import (
    create_catalog_agent,
    headers,
    publish_connector,
    publish_version,
    unique_slug,
    workspace,
)
from conftest import PLATFORM_ADMIN
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.asyncio import Redis
from test_executor import Adapter
from test_tool_governance_integration import clear_unpublished_outbox

from nexora_api.browser_mcp import BROWSER_SERVER_KEY
from nexora_api.connector_mcp import CONNECTOR_SERVER_KEY
from nexora_api.executor import DurableAgentExecutor, ExecutionProfile
from nexora_api.executor_store import ExecutorStore
from nexora_api.main import create_app
from nexora_api.mcp_gateway import McpGateway
from nexora_api.migrate import migrate
from nexora_api.model_routing import (
    ModelCandidate,
    ModelCapability,
    ModelRouter,
    ProviderResponse,
    ProviderToolCall,
    ProviderUsage,
)
from nexora_api.outbox import QUEUE_STREAM
from nexora_api.task_mcp import TASK_SERVER_KEY, TaskMcpAdapter
from nexora_api.worker import AgentWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]

DEMO_SENTINEL = "NEXORA_CUSTOMER_DEMO_SENTINEL"


class DemoConnectorAdapter:
    """Deterministic provider fixture behind the real governed connector boundary."""

    def __init__(self):
        self.calls = []
        self.posts = []
        self.comments = {
            "comment-42": {
                "id": "comment-42",
                "message": "Do you deliver on Saturdays?",
                "replies": [],
            }
        }

    async def call_tool_for_context(
        self, context, remote_name, arguments, timeout_seconds
    ):
        del timeout_seconds
        capability = remote_name.split(":", 1)[1]
        self.calls.append((str(context.run_id), capability, dict(arguments)))
        if capability == "posts.read":
            return {"posts": list(self.posts)}
        if capability == "posts.create":
            post = {
                "id": "draft-1001",
                "title": arguments["title"],
                "content": arguments["content"],
                "status": arguments["status"],
            }
            self.posts.append(post)
            return post
        if capability == "comments.read":
            return {"comments": list(self.comments.values())}
        if capability == "comments.reply":
            comment = self.comments[arguments["comment_id"]]
            reply = {"id": "reply-9001", "message": arguments["message"]}
            comment["replies"].append(reply)
            return reply
        raise AssertionError(f"unexpected demo connector capability: {capability}")


class DemoBrowserAdapter:
    def __init__(self):
        self.calls = []

    async def call_tool_for_context(
        self, context, remote_name, arguments, timeout_seconds
    ):
        del timeout_seconds
        assert remote_name == "page.inspect"
        assert context.agent_snapshot["browser"]["allowed_origin"] == "https://demo.example"
        self.calls.append((str(context.run_id), dict(arguments)))
        return {
            "ok": True,
            "url": "https://demo.example/customer-proof",
            "status_code": 200,
            "title": "Nexora customer proof",
            "text": f"Rendered customer page: {DEMO_SENTINEL}",
            "links": [],
            "truncated": False,
        }


class ScenarioAdapter(Adapter):
    """Scripted model turns that still consume and validate real tool observations."""

    def __init__(self, steps):
        self.steps = iter(steps)
        self.requests = []

    async def generate(self, **kwargs):
        request = kwargs["request"]
        self.requests.append(request)
        step = next(self.steps)
        return step(request)


def _call(call_id, name, arguments):
    return ProviderResponse(
        None,
        (ProviderToolCall(call_id, name, arguments),),
        None,
        ProviderUsage(10, 1),
        "tool_call",
    )


def _stop(text):
    return ProviderResponse(text, (), None, ProviderUsage(10, 1), "stop")


def _tool_payloads(request):
    result = []
    for message in request.messages:
        if message.role != "tool":
            continue
        try:
            result.append(json.loads(message.content))
        except json.JSONDecodeError:
            continue
    return result


def _task_id(request):
    for payload in _tool_payloads(request):
        if isinstance(payload, dict) and payload.get("task_id"):
            return payload["task_id"]
    raise AssertionError("task tool result missing from model context")


def _contains(request, key, value):
    return any(
        isinstance(payload, dict)
        and (
            payload.get(key) == value
            or value in json.dumps(payload, sort_keys=True)
        )
        for payload in _tool_payloads(request)
    )


def seo_provider():
    def verified(request):
        assert _contains(request, "id", "draft-1001")
        return _call(
            "seo-verify",
            "nexora.tasks.verify",
            {
                "task_id": _task_id(request),
                "verification_tool": "wordpress.posts.read",
                "summary": "The created WordPress draft is visible in a fresh provider read.",
                "satisfied": True,
                "idempotency_key": "seo-verify-0001",
            },
        )

    return ScenarioAdapter(
        [
            lambda _request: _call("seo-audit", "wordpress.posts.read", {}),
            lambda _request: _call(
                "seo-task",
                "nexora.tasks.create",
                {
                    "title": "Create the approved SEO draft",
                    "description": (
                        "Persist the customer-approved SEO improvement as a WordPress draft."
                    ),
                    "action_tool": "wordpress.posts.create",
                    "idempotency_key": "seo-task-0001",
                },
            ),
            lambda _request: _call(
                "seo-write",
                "wordpress.posts.create",
                {
                    "title": "Customer-ready SEO draft",
                    "content": "Verified draft content created by the release acceptance flow.",
                    "status": "draft",
                    "idempotency_key": "seo-write-0001",
                },
            ),
            lambda request: (
                _call("seo-readback", "wordpress.posts.read", {})
                if _contains(request, "id", "draft-1001")
                else (_ for _ in ()).throw(AssertionError("approved draft result missing"))
            ),
            verified,
            lambda request: (
                _stop("SEO draft created and verified.")
                if any(
                    payload.get("verification_state") == "verified"
                    for payload in _tool_payloads(request)
                    if isinstance(payload, dict)
                )
                else (_ for _ in ()).throw(AssertionError("SEO verification result missing"))
            ),
        ]
    )


def social_provider():
    def verified(request):
        assert _contains(request, "id", "reply-9001")
        return _call(
            "social-verify",
            "nexora.tasks.verify",
            {
                "task_id": _task_id(request),
                "verification_tool": "instagram.comments.read",
                "summary": "The approved reply is visible in a fresh comment read.",
                "satisfied": True,
                "idempotency_key": "social-verify-0001",
            },
        )

    return ScenarioAdapter(
        [
            lambda _request: _call("social-read", "instagram.comments.read", {}),
            lambda _request: _call(
                "social-task",
                "nexora.tasks.create",
                {
                    "title": "Reply to the customer comment",
                    "description": "Answer the Saturday delivery question after approval.",
                    "action_tool": "instagram.comments.reply",
                    "idempotency_key": "social-task-0001",
                },
            ),
            lambda _request: _call(
                "social-reply",
                "instagram.comments.reply",
                {
                    "comment_id": "comment-42",
                    "message": "Yes, Saturday delivery is available by appointment.",
                    "idempotency_key": "social-reply-0001",
                },
            ),
            lambda request: (
                _call("social-readback", "instagram.comments.read", {})
                if _contains(request, "id", "reply-9001")
                else (_ for _ in ()).throw(AssertionError("approved reply result missing"))
            ),
            verified,
            lambda request: (
                _stop("Instagram reply sent and verified.")
                if any(
                    payload.get("verification_state") == "verified"
                    for payload in _tool_payloads(request)
                    if isinstance(payload, dict)
                )
                else (_ for _ in ()).throw(AssertionError("social verification result missing"))
            ),
        ]
    )


def reporting_provider():
    return ScenarioAdapter(
        [
            lambda _request: _call(
                "report-task",
                "nexora.tasks.create",
                {
                    "title": "Review the weekly conversion drop",
                    "description": (
                        "Investigate the measured conversion drop before any external change."
                    ),
                    "idempotency_key": "report-task-0001",
                },
            ),
            lambda request: (
                _stop("Report complete; the follow-up is persisted without an external mutation.")
                if _task_id(request)
                else (_ for _ in ()).throw(AssertionError("report follow-up missing"))
            ),
        ]
    )


def browser_provider():
    return ScenarioAdapter(
        [
            lambda _request: _call(
                "browser-inspect", "browser.page.inspect", {"path": "/customer-proof"}
            ),
            lambda request: (
                _stop(f"Browser evidence captured: {DEMO_SENTINEL}")
                if _contains(request, "text", DEMO_SENTINEL)
                else (_ for _ in ()).throw(AssertionError("browser evidence missing"))
            ),
        ]
    )


def _connect(client, owner, workspace_id, definition_id, credentials, account):
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/integrations",
        json={
            "integration_definition_id": definition_id,
            "display_name": f"{definition_id} demo",
            "account_identifier": account,
            "credentials": credentials,
        },
        headers=owner,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _publish_agent(
    client,
    admin,
    package,
    slug,
    required_integrations,
    required_tools,
    **overrides,
):
    create_catalog_agent(client, admin, package, slug)
    return publish_version(
        client,
        admin,
        package,
        slug,
        "1.0.0",
        required_integrations=required_integrations,
        optional_integrations=[],
        required_tools=required_tools,
        optional_tools=[],
        model_policy={"primary": "demo"},
        **overrides,
    )


def _install(client, owner, workspace_id, slug, bindings=None):
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/tenant-agents",
        json={"slug": slug, "bindings": bindings or {}},
        headers=owner,
    )
    assert response.status_code == 201, response.text
    assert response.json()["readiness"]["ready"] is True
    return response.json()


def _create_run(client, owner, workspace_id, agent_id, prompt, key):
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/tenant-agents/{agent_id}/runs",
        json={"input": prompt},
        headers={**owner, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _executor(settings, workspace_id, provider, connector, browser):
    gateway = McpGateway(
        settings,
        adapters={
            CONNECTOR_SERVER_KEY: connector,
            TASK_SERVER_KEY: TaskMcpAdapter(settings),
            BROWSER_SERVER_KEY: browser,
        },
    )
    return DurableAgentExecutor(
        store=ExecutorStore(settings),
        router=ModelRouter(
            [ModelCandidate("test", "customer-demo-model", frozenset(ModelCapability))]
        ),
        adapters={"test": provider},
        gateway=gateway,
        profiles={
            "demo": ExecutionProfile(
                frozenset({UUID(workspace_id)}),
                frozenset({"test"}),
                allowed_server_keys=frozenset(
                    {CONNECTOR_SERVER_KEY, TASK_SERVER_KEY, BROWSER_SERVER_KEY}
                ),
                max_steps=16,
            )
        },
    )


def _process_once(settings, executor, worker_id):
    async def exercise():
        redis = Redis.from_url(settings.redis_url.get_secret_value())
        try:
            worker = AgentWorker(
                settings,
                redis,
                executor,
                worker_id=worker_id,
                lease_seconds=6,
            )
            return await worker.process_once(100)
        finally:
            await redis.aclose()

    return asyncio.run(exercise())


def _reset_queue(settings):
    async def exercise():
        redis = Redis.from_url(settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
        finally:
            await redis.aclose()

    asyncio.run(exercise())


def _approve(client, owner, workspace_id, run_id, expected_action):
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/approvals?limit=100",
        headers=owner,
    )
    assert response.status_code == 200, response.text
    approvals = [
        item
        for item in response.json()["items"]
        if item["run_id"] == run_id and item["status"] == "pending"
    ]
    assert len(approvals) == 1, approvals
    approval = approvals[0]
    assert approval["requested_action"] == expected_action
    decided = client.post(
        f"/api/v1/workspaces/{workspace_id}/approvals/{approval['id']}/decision",
        json={"decision": "approved"},
        headers=owner,
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"


def _assert_verified_task(client, owner, workspace_id, run_id, action_tool):
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/tasks?limit=100",
        headers=owner,
    )
    assert response.status_code == 200, response.text
    follow_ups = [item for item in response.json()["items"] if item["kind"] == "follow_up"]
    assert len(follow_ups) == 1, follow_ups
    task = follow_ups[0]
    assert task["action_tool_name"] == action_tool
    assert task["status"] == "succeeded"
    assert task["verification_state"] == "verified"
    assert len(task["evidence"]) == 1
    assert task["evidence"][0]["satisfied"] is True


def _action_names(client, owner, workspace_id, run_id):
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/actions?limit=100",
        headers=owner,
    )
    assert response.status_code == 200, response.text
    return [
        (item["action_name"], item["side_effect"], item["status"])
        for item in response.json()["items"]
    ]


def test_customer_demo_workspace_completes_release_gate(keys, platform_settings):
    settings = platform_settings.model_copy(
        update={
            "browser_runtime_url": "http://browser.demo.internal:8080",
            "browser_runtime_token": SecretStr("browser-demo-token-" + "x" * 32),
        }
    )
    migrate(settings)
    clear_unpublished_outbox(settings)
    _reset_queue(settings)

    connector = DemoConnectorAdapter()
    browser = DemoBrowserAdapter()

    with TestClient(create_app(settings=settings)) as client:
        admin = headers(keys, PLATFORM_ADMIN, request_id="customer-release-admin")
        owner = headers(keys, "customer-demo-owner", request_id="customer-release-owner")
        workspace_id = workspace(client, owner, "Customer Release Acceptance")

        publish_connector(client, admin, "wordpress")
        publish_connector(
            client,
            admin,
            "instagram",
            auth="bearer_token",
            oauth=None,
            scopes=[],
            credential_fields=[
                {
                    "key": "access_token",
                    "label": "Access token",
                    "secret": True,
                    "required": True,
                }
            ],
        )

        wordpress = _connect(
            client,
            owner,
            workspace_id,
            "wordpress",
            {
                "site_url": "https://wordpress.demo.example",
                "username": "demo-editor",
                "password": "demo-wordpress-app-password-0001",
            },
            "demo.wordpress",
        )
        instagram = _connect(
            client,
            owner,
            workspace_id,
            "instagram",
            {"access_token": "demo-instagram-token-0001"},
            "@nexora_demo",
        )

        seo_slug = unique_slug("customer-seo")
        social_slug = unique_slug("customer-social")
        reporting_slug = unique_slug("customer-reporting")
        browser_slug = unique_slug("customer-browser")

        _publish_agent(
            client,
            admin,
            "seo",
            seo_slug,
            ["wordpress"],
            [
                "wordpress.posts.read",
                "wordpress.posts.create",
                "nexora.tasks.create",
                "nexora.tasks.verify",
            ],
        )
        _publish_agent(
            client,
            admin,
            "social-media",
            social_slug,
            ["instagram"],
            [
                "instagram.comments.read",
                "instagram.comments.reply",
                "nexora.tasks.create",
                "nexora.tasks.verify",
            ],
            settings_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        )
        _publish_agent(
            client,
            admin,
            "reporting",
            reporting_slug,
            [],
            ["nexora.tasks.create"],
        )
        _publish_agent(
            client,
            admin,
            "seo",
            browser_slug,
            [],
            ["browser.page.inspect"],
            settings_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "site_url": {"type": "string", "maxLength": 200}
                },
                "required": ["site_url"],
            },
        )

        seo = _install(
            client,
            owner,
            workspace_id,
            seo_slug,
            {"wordpress": wordpress["id"]},
        )
        social = _install(
            client,
            owner,
            workspace_id,
            social_slug,
            {"instagram": instagram["id"]},
        )
        reporting = _install(client, owner, workspace_id, reporting_slug)
        browser_agent = _install(client, owner, workspace_id, browser_slug)
        configured = client.patch(
            f"/api/v1/workspaces/{workspace_id}/tenant-agents/{browser_agent['id']}",
            json={"settings": {"site_url": "https://demo.example/customer-proof"}},
            headers=owner,
        )
        assert configured.status_code == 200, configured.text

        # 1. SEO audit -> durable task -> approval -> WordPress draft -> fresh read -> verification.
        clear_unpublished_outbox(settings)
        _reset_queue(settings)
        seo_run = _create_run(
            client,
            owner,
            workspace_id,
            seo["id"],
            "Audit the site, create the approved SEO draft, then verify it.",
            "customer-seo-run-0001",
        )
        seo_model = seo_provider()
        seo_executor = _executor(settings, workspace_id, seo_model, connector, browser)
        assert _process_once(settings, seo_executor, "customer-seo-worker")
        paused = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{seo_run}", headers=owner
        ).json()
        assert paused["status"] == "waiting_for_approval"
        assert connector.posts == []
        _approve(client, owner, workspace_id, seo_run, "wordpress.posts.create")
        assert _process_once(settings, seo_executor, "customer-seo-worker-resume")
        seo_result = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{seo_run}/result", headers=owner
        )
        assert seo_result.status_code == 200, seo_result.text
        assert seo_result.json()["status"] == "succeeded"
        assert connector.posts == [
            {
                "id": "draft-1001",
                "title": "Customer-ready SEO draft",
                "content": "Verified draft content created by the release acceptance flow.",
                "status": "draft",
            }
        ]
        _assert_verified_task(
            client, owner, workspace_id, seo_run, "wordpress.posts.create"
        )
        seo_actions = _action_names(client, owner, workspace_id, seo_run)
        assert ("wordpress.posts.create", "write", "succeeded") in seo_actions
        assert sum(
            1
            for name, effect, status in seo_actions
            if name == "wordpress.posts.read" and effect == "read" and status == "succeeded"
        ) == 2

        # 2. Social read -> durable task -> approval -> reply -> fresh read -> verification.
        clear_unpublished_outbox(settings)
        _reset_queue(settings)
        social_run = _create_run(
            client,
            owner,
            workspace_id,
            social["id"],
            "Review the customer comment, reply after approval, then verify the reply.",
            "customer-social-run-0001",
        )
        social_model = social_provider()
        social_executor = _executor(settings, workspace_id, social_model, connector, browser)
        assert _process_once(settings, social_executor, "customer-social-worker")
        paused = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{social_run}", headers=owner
        ).json()
        assert paused["status"] == "waiting_for_approval"
        assert connector.comments["comment-42"]["replies"] == []
        _approve(client, owner, workspace_id, social_run, "instagram.comments.reply")
        assert _process_once(settings, social_executor, "customer-social-worker-resume")
        social_result = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{social_run}/result", headers=owner
        )
        assert social_result.status_code == 200, social_result.text
        assert social_result.json()["status"] == "succeeded"
        assert connector.comments["comment-42"]["replies"] == [
            {
                "id": "reply-9001",
                "message": "Yes, Saturday delivery is available by appointment.",
            }
        ]
        _assert_verified_task(
            client, owner, workspace_id, social_run, "instagram.comments.reply"
        )
        social_actions = _action_names(client, owner, workspace_id, social_run)
        assert ("instagram.comments.reply", "external_communication", "succeeded") in social_actions
        assert sum(
            1
            for name, effect, status in social_actions
            if name == "instagram.comments.read"
            and effect == "read"
            and status == "succeeded"
        ) == 2

        # 3. Read-only reporting persists a follow-up and never performs an external mutation.
        clear_unpublished_outbox(settings)
        _reset_queue(settings)
        reporting_run = _create_run(
            client,
            owner,
            workspace_id,
            reporting["id"],
            (
                "Create a durable follow-up for the measured conversion drop; "
                "do not change any external system."
            ),
            "customer-report-run-0001",
        )
        report_model = reporting_provider()
        report_executor = _executor(settings, workspace_id, report_model, connector, browser)
        assert _process_once(settings, report_executor, "customer-report-worker")
        report_result = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{reporting_run}/result", headers=owner
        )
        assert report_result.status_code == 200, report_result.text
        assert report_result.json()["status"] == "succeeded"
        tasks = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{reporting_run}/tasks?limit=100",
            headers=owner,
        ).json()["items"]
        follow_ups = [task for task in tasks if task["kind"] == "follow_up"]
        assert len(follow_ups) == 1
        assert follow_ups[0]["action_tool_name"] is None
        report_actions = _action_names(client, owner, workspace_id, reporting_run)
        assert report_actions == [("nexora.tasks.create", "write", "succeeded")]

        # 4. Browser inspection must surface evidence from the allowlisted rendered page.
        clear_unpublished_outbox(settings)
        _reset_queue(settings)
        browser_run = _create_run(
            client,
            owner,
            workspace_id,
            browser_agent["id"],
            "Inspect the allowlisted customer proof page and report its sentinel.",
            "customer-browser-run-0001",
        )
        browser_model = browser_provider()
        browser_executor = _executor(settings, workspace_id, browser_model, connector, browser)
        assert _process_once(settings, browser_executor, "customer-browser-worker")
        browser_result = client.get(
            f"/api/v1/workspaces/{workspace_id}/runs/{browser_run}/result", headers=owner
        )
        assert browser_result.status_code == 200, browser_result.text
        assert browser_result.json()["status"] == "succeeded"
        assert DEMO_SENTINEL in browser_result.json()["output_text"]
        browser_actions = _action_names(client, owner, workspace_id, browser_run)
        assert browser_actions == [("browser.page.inspect", "read", "succeeded")]
        assert browser.calls == [(browser_run, {"path": "/customer-proof"})]

        # Every release-gate run reached a durable terminal state with its own audit history.
        with psycopg.connect(settings.database_url.get_secret_value()) as connection:
            rows = connection.execute(
                """SELECT id,status FROM agent_runs
                   WHERE workspace_id=%s AND id=ANY(%s::uuid[])
                   ORDER BY id""",
                (workspace_id, [seo_run, social_run, reporting_run, browser_run]),
            ).fetchall()
            assert len(rows) == 4
            assert {row[1] for row in rows} == {"succeeded"}
