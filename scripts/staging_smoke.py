#!/usr/bin/env python3
"""Run a bounded end-to-end smoke test against a deployed Nexora staging environment.

The script deliberately uses only the Python standard library so the staging gate has no
runtime package-install step. Credentials are accepted only through environment variables
and are never printed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

TERMINAL = {"succeeded", "failed", "cancelled"}
REQUIRED_EVENT_TYPES = {"run.queued", "run.started", "run.succeeded"}
MAX_RESPONSE_BYTES = 256 * 1024


class SmokeError(RuntimeError):
    pass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise SmokeError(f"{name} must be a boolean")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SmokeError(f"missing required environment variable: {name}")
    return value


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "")
    value = default if not raw else float(raw)
    if not minimum <= value <= maximum:
        raise SmokeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _base_url(name: str, required: bool = True) -> str | None:
    raw = _required(name) if required else os.getenv(name, "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    allow_http = _env_bool("NEXORA_STAGING_ALLOW_HTTP", False)
    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise SmokeError(f"{name} must be an origin URL without credentials, path, query or fragment")
    return raw.rstrip("/")


def _uuid(name: str) -> str:
    raw = _required(name)
    try:
        UUID(raw)
    except ValueError as exc:
        raise SmokeError(f"{name} must be a UUID") from exc
    return raw


def _optional_tool(name: str) -> str | None:
    value = os.getenv(name, "").strip() or None
    if value and not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", value):
        raise SmokeError(f"{name} is not a valid tool name")
    return value


def _tool_names(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return ()
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    if len(values) > 20 or not values:
        raise SmokeError(f"{name} must contain between 1 and 20 tool names")
    for value in values:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", value):
            raise SmokeError(f"{name} contains an invalid tool name")
    return values


@dataclass(frozen=True)
class Config:
    api_url: str
    access_token: str
    workspace_id: str
    agent_id: str
    agent_kind: str
    prompt: str
    expected_text: str | None
    expected_source_key: str | None
    approval_tool: str | None
    expected_tools: tuple[str, ...]
    require_task_graph: bool
    require_verified_task: bool
    expected_verified_action_tool: str | None
    forbid_external_mutations: bool
    timeout_seconds: float
    poll_seconds: float
    require_retrieval: bool
    require_observability: bool
    worker_admin_url: str | None
    metrics_token: str | None

    @classmethod
    def from_env(cls) -> "Config":
        approval_tool = _optional_tool("NEXORA_STAGING_APPROVAL_TOOL")
        agent_kind = os.getenv("NEXORA_STAGING_AGENT_KIND", "custom").strip().lower()
        if agent_kind not in {"custom", "standard"}:
            raise SmokeError("NEXORA_STAGING_AGENT_KIND must be custom or standard")
        expected_verified_action_tool = _optional_tool(
            "NEXORA_STAGING_EXPECT_VERIFIED_ACTION_TOOL"
        )
        require_observability = _env_bool("NEXORA_STAGING_REQUIRE_OBSERVABILITY", False)
        worker_admin_url = _base_url(
            "NEXORA_STAGING_WORKER_ADMIN_URL", required=require_observability
        )
        metrics_token = (
            _required("NEXORA_STAGING_METRICS_TOKEN")
            if require_observability
            else os.getenv("NEXORA_STAGING_METRICS_TOKEN", "").strip() or None
        )
        prompt = (
            os.getenv("NEXORA_STAGING_PROMPT", "").strip()
            or "Answer this staging health-check request concisely."
        )
        if len(prompt) > 20000:
            raise SmokeError("NEXORA_STAGING_PROMPT must be at most 20000 characters")
        return cls(
            api_url=_base_url("NEXORA_STAGING_API_URL") or "",
            access_token=_required("NEXORA_STAGING_ACCESS_TOKEN"),
            workspace_id=_uuid("NEXORA_STAGING_WORKSPACE_ID"),
            agent_id=_uuid("NEXORA_STAGING_AGENT_ID"),
            agent_kind=agent_kind,
            prompt=prompt,
            expected_text=os.getenv("NEXORA_STAGING_EXPECT_TEXT", "").strip() or None,
            expected_source_key=os.getenv(
                "NEXORA_STAGING_EXPECT_SOURCE_KEY", ""
            ).strip()
            or None,
            approval_tool=approval_tool,
            expected_tools=_tool_names("NEXORA_STAGING_EXPECT_TOOLS"),
            require_task_graph=_env_bool("NEXORA_STAGING_REQUIRE_TASK_GRAPH", False),
            require_verified_task=_env_bool("NEXORA_STAGING_REQUIRE_VERIFIED_TASK", False),
            expected_verified_action_tool=expected_verified_action_tool,
            forbid_external_mutations=_env_bool(
                "NEXORA_STAGING_FORBID_EXTERNAL_MUTATIONS", False
            ),
            timeout_seconds=_bounded_float(
                "NEXORA_STAGING_TIMEOUT_SECONDS", 180.0, 10.0, 600.0
            ),
            poll_seconds=_bounded_float("NEXORA_STAGING_POLL_SECONDS", 2.0, 0.25, 10.0),
            require_retrieval=_env_bool("NEXORA_STAGING_REQUIRE_RETRIEVAL", True),
            require_observability=require_observability,
            worker_admin_url=worker_admin_url,
            metrics_token=metrics_token,
        )


class Client:
    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(
        self, *, auth: bool, json_body: bool = False, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "nexora-staging-smoke/1"}
        if auth:
            if not self.token:
                raise SmokeError("authenticated request has no token")
            headers["Authorization"] = f"Bearer {self.token}"
        if json_body:
            headers["Content-Type"] = "application/json"
        if extra:
            headers.update(extra)
        return headers

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        auth: bool = True,
        expected: tuple[int, ...] = (200,),
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        request = Request(
            self.base_url + path,
            method=method,
            data=data,
            headers=self._headers(auth=auth, json_body=body is not None, extra=headers),
        )
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            status = exc.code
        except URLError as exc:
            raise SmokeError(f"request failed for {path}: {exc.reason.__class__.__name__}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SmokeError(f"response too large for {path}")
        if status not in expected:
            code = "unexpected_response"
            try:
                parsed = json.loads(raw or b"{}")
                code = str(parsed.get("error", {}).get("code", code))
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
            raise SmokeError(f"{method} {path} returned HTTP {status} ({code})")
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SmokeError(f"{method} {path} returned invalid JSON") from exc

    def request_text(
        self,
        path: str,
        *,
        auth: bool = False,
        expected: tuple[int, ...] = (200,),
    ) -> str:
        request = Request(
            self.base_url + path,
            method="GET",
            headers=self._headers(auth=auth),
        )
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            status = exc.code
        except URLError as exc:
            raise SmokeError(f"request failed for {path}: {exc.reason.__class__.__name__}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SmokeError(f"response too large for {path}")
        if status not in expected:
            raise SmokeError(f"GET {path} returned HTTP {status}")
        return raw.decode("utf-8", errors="strict")


def metric_sum(text: str, name: str) -> float:
    total = 0.0
    pattern = re.compile(rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([^\s]+)$")
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        try:
            total += float(match.group(1))
        except ValueError:
            continue
    return total


def validate_events(events: list[dict[str, Any]], *, approval_expected: bool) -> list[str]:
    if not events:
        raise SmokeError("run returned no events")
    numbers = [int(event["event_no"]) for event in events]
    if numbers != list(range(1, len(numbers) + 1)):
        raise SmokeError("run event sequence is not contiguous from event 1")
    event_types = [str(event["event_type"]) for event in events]
    missing = REQUIRED_EVENT_TYPES - set(event_types)
    if missing:
        raise SmokeError("run is missing required events: " + ", ".join(sorted(missing)))
    if approval_expected:
        for required in ("tool.approval_requested", "run.waiting_for_approval"):
            if required not in event_types:
                raise SmokeError(f"approval flow is missing {required}")
        if event_types.count("run.started") < 2:
            raise SmokeError("approval flow did not resume through a second worker attempt")
    return event_types


def _list_sources(api: Client, workspace_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(20):
        query = "?limit=100" + (f"&cursor={cursor}" if cursor else "")
        _, page = api.request_json(
            "GET", f"/api/v1/workspaces/{workspace_id}/knowledge/sources{query}"
        )
        items.extend(page.get("items", []))
        cursor = page.get("next_cursor")
        if not cursor:
            return items
    raise SmokeError("knowledge source pagination exceeded safety bound")


def _list_events(api: Client, workspace_id: str, run_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = 0
    for _ in range(20):
        query = urlencode({"limit": 100, "cursor": cursor})
        _, page = api.request_json(
            "GET", f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/events?{query}"
        )
        items.extend(page.get("items", []))
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            return items
        cursor = int(next_cursor)
    raise SmokeError("run event pagination exceeded safety bound")


def _list_run_actions(api: Client, workspace_id: str, run_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(20):
        query = "?limit=100" + (f"&cursor={cursor}" if cursor else "")
        _, page = api.request_json(
            "GET", f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/actions{query}"
        )
        items.extend(page.get("items", []))
        cursor = page.get("next_cursor")
        if not cursor:
            return items
    raise SmokeError("run action pagination exceeded safety bound")


def _list_run_tasks(api: Client, workspace_id: str, run_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(20):
        query = "?limit=100" + (f"&cursor={cursor}" if cursor else "")
        _, page = api.request_json(
            "GET", f"/api/v1/workspaces/{workspace_id}/runs/{run_id}/tasks{query}"
        )
        items.extend(page.get("items", []))
        cursor = page.get("next_cursor")
        if not cursor:
            return items
    raise SmokeError("run task pagination exceeded safety bound")


def validate_release_evidence(
    actions: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    expected_tools: tuple[str, ...],
    require_task_graph: bool,
    require_verified_task: bool,
    expected_verified_action_tool: str | None,
    forbid_external_mutations: bool,
) -> dict[str, int]:
    succeeded_tools = {
        str(action.get("action_name"))
        for action in actions
        if action.get("status") == "succeeded"
    }
    missing_tools = sorted(set(expected_tools) - succeeded_tools)
    if missing_tools:
        raise SmokeError(
            "expected tools did not complete successfully: " + ", ".join(missing_tools)
        )

    if forbid_external_mutations:
        unsafe = [
            str(action.get("action_name"))
            for action in actions
            if action.get("status") == "succeeded"
            and action.get("side_effect") != "read"
            and not str(action.get("action_name", "")).startswith("nexora.tasks.")
        ]
        if unsafe:
            raise SmokeError(
                "read-only release scenario performed external mutations: "
                + ", ".join(sorted(unsafe))
            )

    follow_ups = [task for task in tasks if task.get("kind") == "follow_up"]
    if require_task_graph and not follow_ups:
        raise SmokeError("release scenario persisted no follow-up task")

    verified = [
        task
        for task in follow_ups
        if task.get("status") == "succeeded"
        and task.get("verification_state") == "verified"
        and any(evidence.get("satisfied") is True for evidence in task.get("evidence", []))
    ]
    if expected_verified_action_tool:
        verified = [
            task
            for task in verified
            if task.get("action_tool_name") == expected_verified_action_tool
        ]
    if require_verified_task and not verified:
        raise SmokeError("release scenario persisted no verified completed follow-up task")

    return {
        "action_count": len(actions),
        "task_count": len(tasks),
        "follow_up_count": len(follow_ups),
        "verified_task_count": len(verified),
    }


def _find_pending_approval(
    api: Client, workspace_id: str, run_id: str, approval_tool: str
) -> dict[str, Any] | None:
    cursor: str | None = None
    for _ in range(20):
        query = "?limit=100" + (f"&cursor={cursor}" if cursor else "")
        _, page = api.request_json(
            "GET", f"/api/v1/workspaces/{workspace_id}/approvals{query}"
        )
        for approval in page.get("items", []):
            if (
                approval.get("run_id") == run_id
                and approval.get("requested_action") == approval_tool
                and approval.get("status") == "pending"
            ):
                return approval
        cursor = page.get("next_cursor")
        if not cursor:
            return None
    raise SmokeError("approval pagination exceeded safety bound")


def _worker_metrics(config: Config) -> tuple[Client | None, dict[str, float]]:
    if not config.worker_admin_url or not config.metrics_token:
        if config.require_observability:
            raise SmokeError("observability is required but worker admin configuration is missing")
        return None, {}
    worker = Client(config.worker_admin_url, config.metrics_token)
    _, ready = worker.request_json("GET", "/api/v1/health/ready", auth=False)
    if ready.get("status") != "ok":
        raise SmokeError("worker readiness is not ok")
    metrics = worker.request_text("/metrics", auth=True)
    names = [
        "nexora_agent_runs_total",
        "nexora_model_calls_total",
        "nexora_retrieval_queries_total",
        "nexora_tool_calls_total",
    ]
    return worker, {name: metric_sum(metrics, name) for name in names}


def _wait_metric_deltas(
    worker: Client,
    before: dict[str, float],
    required: list[str],
    *,
    timeout_seconds: float = 15.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    missing = list(required)
    while time.monotonic() < deadline:
        metrics = worker.request_text("/metrics", auth=True)
        missing = [
            name
            for name in required
            if metric_sum(metrics, name) <= before.get(name, 0.0)
        ]
        if not missing:
            return
        time.sleep(0.5)
    raise SmokeError(
        "expected worker metrics to increase during the smoke run: "
        + ", ".join(missing)
    )


def run(config: Config) -> dict[str, Any]:
    api = Client(config.api_url, config.access_token)
    created_run_id: str | None = None
    terminal = False
    approval_decided = False

    _, live = api.request_json("GET", "/api/v1/health/live", auth=False)
    _, ready = api.request_json("GET", "/api/v1/health/ready", auth=False)
    if live.get("status") != "ok" or ready.get("status") != "ok":
        raise SmokeError("API health is not ok")

    _, me = api.request_json("GET", "/api/v1/me")
    if not isinstance(me.get("issuer"), str) or not isinstance(me.get("subject"), str):
        raise SmokeError("authenticated identity response is invalid")

    _, workspace = api.request_json(
        "GET", f"/api/v1/workspaces/{config.workspace_id}"
    )
    if workspace.get("id") != config.workspace_id:
        raise SmokeError("staging workspace identity mismatch")

    _, agent = api.request_json(
        "GET",
        f"/api/v1/workspaces/{config.workspace_id}/agents/{config.agent_id}",
    )
    if agent.get("id") != config.agent_id:
        raise SmokeError("staging agent identity mismatch")

    if config.expected_source_key:
        sources = _list_sources(api, config.workspace_id)
        if not any(source.get("source_key") == config.expected_source_key for source in sources):
            raise SmokeError("expected staging knowledge source is not visible")

    worker, metrics_before = _worker_metrics(config)

    idempotency_key = "staging-" + uuid4().hex
    body = {"agent_id": config.agent_id, "input": config.prompt}
    status, created = api.request_json(
        "POST",
        f"/api/v1/workspaces/{config.workspace_id}/runs",
        body=body,
        headers={"Idempotency-Key": idempotency_key},
        expected=(201,),
    )
    if status != 201:
        raise SmokeError("new run did not return HTTP 201")
    created_run_id = str(created.get("id", ""))
    try:
        UUID(created_run_id)
    except ValueError as exc:
        raise SmokeError("run creation returned an invalid run id") from exc
    trace_id = str(created.get("trace_id", ""))
    UUID(trace_id)

    replay_status, replay = api.request_json(
        "POST",
        f"/api/v1/workspaces/{config.workspace_id}/runs",
        body=body,
        headers={"Idempotency-Key": idempotency_key},
        expected=(200,),
    )
    if replay_status != 200 or replay.get("id") != created_run_id:
        raise SmokeError("idempotent run replay did not return the original run")

    deadline = time.monotonic() + config.timeout_seconds
    run_state = created
    try:
        while time.monotonic() < deadline:
            _, run_state = api.request_json(
                "GET",
                f"/api/v1/workspaces/{config.workspace_id}/runs/{created_run_id}",
            )
            status = run_state.get("status")
            if status == "waiting_for_approval":
                if not config.approval_tool:
                    raise SmokeError("run unexpectedly requires human approval")
                approval = _find_pending_approval(
                    api, config.workspace_id, created_run_id, config.approval_tool
                )
                if approval and not approval_decided:
                    api.request_json(
                        "POST",
                        (
                            f"/api/v1/workspaces/{config.workspace_id}/approvals/"
                            f"{approval['id']}/decision"
                        ),
                        body={"decision": "approved"},
                    )
                    approval_decided = True
            if status in TERMINAL:
                terminal = True
                break
            time.sleep(config.poll_seconds)
        else:
            raise SmokeError("staging run did not become terminal before the deadline")

        if run_state.get("status") != "succeeded":
            raise SmokeError(
                f"staging run ended as {run_state.get('status')} "
                f"({run_state.get('failure_code') or 'no_failure_code'})"
            )

        _, result = api.request_json(
            "GET",
            f"/api/v1/workspaces/{config.workspace_id}/runs/{created_run_id}/result",
        )
        if result.get("status") != "succeeded" or result.get("trace_id") != trace_id:
            raise SmokeError("terminal result identity does not match the run")
        if not isinstance(result.get("output_text"), str) or not result["output_text"].strip():
            raise SmokeError("successful run returned no final output")
        if config.expected_text and config.expected_text not in result["output_text"]:
            raise SmokeError("successful run output did not contain the configured sentinel")
        if not result.get("model_steps"):
            raise SmokeError("successful run persisted no model steps")
        if config.approval_tool and config.approval_tool not in result.get("selected_tools", []):
            raise SmokeError("approved staging tool is missing from the persisted model selection")

        events = _list_events(api, config.workspace_id, created_run_id)
        event_types = validate_events(events, approval_expected=bool(config.approval_tool))
        if config.approval_tool and not approval_decided:
            raise SmokeError("approval flow completed without this smoke gate deciding the approval")

        if worker:
            required_metrics = [
                "nexora_agent_runs_total",
                "nexora_model_calls_total",
            ]
            if config.require_retrieval:
                required_metrics.append("nexora_retrieval_queries_total")
            if config.approval_tool:
                required_metrics.append("nexora_tool_calls_total")
            _wait_metric_deltas(worker, metrics_before, required_metrics)
        elif config.require_retrieval:
            raise SmokeError(
                "retrieval verification requires NEXORA_STAGING_WORKER_ADMIN_URL "
                "and NEXORA_STAGING_METRICS_TOKEN"
            )

        steps = result.get("model_steps", [])
        return {
            "run_id": created_run_id,
            "trace_id": trace_id,
            "status": result.get("status"),
            "attempt_count": run_state.get("attempt_count"),
            "model_steps": [
                {"provider": step.get("provider"), "model": step.get("model")}
                for step in steps
            ],
            "selected_tools": result.get("selected_tools", []),
            "event_types": event_types,
            "approval_exercised": approval_decided,
            "retrieval_exercised": config.require_retrieval,
            "observability_verified": bool(worker),
        }
    finally:
        if created_run_id and not terminal:
            try:
                api.request_json(
                    "POST",
                    f"/api/v1/workspaces/{config.workspace_id}/runs/{created_run_id}/cancel",
                )
            except Exception:
                pass


def main() -> int:
    try:
        summary = run(Config.from_env())
    except (SmokeError, ValueError) as exc:
        print(f"STAGING SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    print("STAGING SMOKE PASSED")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
