import asyncio
import json
import os

import pytest
from redis.asyncio import Redis
from test_executor import Adapter, response
from test_tool_governance_integration import clear_unpublished_outbox
from test_worker_integration import make_runtime

from nexora_api.migrate import migrate
from nexora_api.outbox import QUEUE_STREAM
from nexora_api.runtime_config import load_runtime_config
from nexora_api.worker_service import WorkerRuntime, build_worker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("NEXORA_INTEGRATION") != "1", reason="Requires PostgreSQL and Redis"
    ),
]


def runtime_config_file(tmp_path, workspace_id):
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps(
            {
                "model_candidates": [
                    {
                        "provider": "test",
                        "model": "operator-model",
                        "capabilities": ["text", "tools"],
                    }
                ],
                "profiles": {
                    "default": {
                        "allowed_workspaces": [workspace_id],
                        "allowed_providers": ["test"],
                        "max_steps": 2,
                    }
                },
            }
        )
    )
    return path


def test_configured_worker_process_runs_a_queued_agent_run(keys, auth_settings, tmp_path):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    client, headers, workspace_id, run_id = make_runtime(keys, auth_settings, "worker-service")
    config = load_runtime_config(runtime_config_file(tmp_path, workspace_id))
    provider = Adapter([response("Answered by the configured worker")])
    settings = auth_settings.model_copy(update={"worker_idle_sleep_seconds": 0.01})

    async def exercise():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = build_worker(
                settings,
                config,
                redis,
                adapters={"test": provider},
                worker_id="service-worker",
            )
            runtime = WorkerRuntime(worker, settings, backoff_base_seconds=0.01)
            for _ in range(10):
                await runtime.run(max_iterations=1)
                state = client.get(
                    f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers()
                ).json()
                if state["status"] != "queued":
                    return state
            raise AssertionError("run was never processed")
        finally:
            await redis.aclose()

    try:
        state = asyncio.run(exercise())
        assert state["status"] == "succeeded", state
        assert len(provider.requests) == 1
    finally:
        client.__exit__(None, None, None)


def test_worker_refuses_workspaces_outside_its_operator_profile(keys, auth_settings, tmp_path):
    migrate(auth_settings)
    clear_unpublished_outbox(auth_settings)
    client, headers, workspace_id, run_id = make_runtime(keys, auth_settings, "worker-unlisted")
    # A profile that does not list this workspace must not execute its runs.
    foreign = "22222222-2222-2222-2222-222222222222"
    config = load_runtime_config(runtime_config_file(tmp_path, foreign))
    provider = Adapter([response("Must never be produced")])
    settings = auth_settings.model_copy(update={"worker_idle_sleep_seconds": 0.01})

    async def exercise():
        redis = Redis.from_url(auth_settings.redis_url.get_secret_value())
        try:
            await redis.delete(QUEUE_STREAM)
            worker = build_worker(
                settings,
                config,
                redis,
                adapters={"test": provider},
                worker_id="unlisted-worker",
            )
            runtime = WorkerRuntime(worker, settings, backoff_base_seconds=0.01)
            for _ in range(10):
                await runtime.run(max_iterations=1)
                state = client.get(
                    f"/api/v1/workspaces/{workspace_id}/runs/{run_id}", headers=headers()
                ).json()
                if state["status"] == "failed":
                    return state
            raise AssertionError("unauthorized run was not rejected")
        finally:
            await redis.aclose()

    try:
        state = asyncio.run(exercise())
        assert state["failure_code"] == "model_profile_not_authorized", state
        assert provider.requests == []
    finally:
        client.__exit__(None, None, None)
