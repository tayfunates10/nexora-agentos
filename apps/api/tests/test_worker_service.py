import asyncio

import pytest
from fastapi.testclient import TestClient
from test_health import StubProbe

from nexora_api.config import Settings
from nexora_api.worker_main import create_admin_app
from nexora_api.worker_service import WorkerRuntime, close_provider_adapters

TOKEN = "worker-scrape-token-long-enough-01"


class FakeWorker:
    """Stands in for AgentWorker so the loop's own behavior is what is measured."""

    def __init__(self, results):
        self.worker_id = "test-worker"
        self.results = list(results)
        self.calls = 0

    async def process_once(self):
        self.calls += 1
        if not self.results:
            return False
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def settings(**overrides):
    return Settings(**{"worker_idle_sleep_seconds": 0.01, **overrides})


def test_loop_processes_until_stopped():
    worker = FakeWorker([True, True, True])
    runtime = WorkerRuntime(worker, settings())

    iterations = asyncio.run(runtime.run(max_iterations=3))

    assert iterations == 3
    assert worker.calls == 3



class FakeAuxWorker:
    def __init__(self, result=True):
        self.result = result
        self.calls = 0

    async def process_once(self):
        self.calls += 1
        return self.result


def test_loop_services_evaluation_judge_jobs_between_agent_iterations():
    worker = FakeWorker([False])
    judge = FakeAuxWorker()
    runtime = WorkerRuntime(worker, settings(), evaluation_judge_worker=judge)

    iterations = asyncio.run(runtime.run(max_iterations=1))

    assert iterations == 1
    assert worker.calls == 1
    assert judge.calls == 1


def test_stop_request_ends_the_loop_between_jobs():
    worker = FakeWorker([True] * 50)
    runtime = WorkerRuntime(worker, settings())

    async def exercise():
        runtime.request_stop()
        return await runtime.run(max_iterations=50)

    assert asyncio.run(exercise()) == 0
    assert worker.calls == 0
    assert runtime.stopping


def test_dependency_failures_back_off_instead_of_killing_the_worker():
    worker = FakeWorker([RuntimeError("redis down"), RuntimeError("redis down"), True])
    runtime = WorkerRuntime(worker, settings(), backoff_base_seconds=0.01)

    iterations = asyncio.run(runtime.run(max_iterations=3))

    assert iterations == 3
    assert worker.calls == 3


def test_shutdown_interrupts_backoff_promptly():
    worker = FakeWorker([RuntimeError("redis down")] * 10)
    runtime = WorkerRuntime(worker, settings())  # production backoff: seconds, not ticks

    async def exercise():
        stopper = asyncio.create_task(stop_soon())
        started = asyncio.get_running_loop().time()
        await runtime.run(max_iterations=10)
        await stopper
        return asyncio.get_running_loop().time() - started

    async def stop_soon():
        await asyncio.sleep(0.05)
        runtime.request_stop()

    # Backoff would otherwise hold the loop for seconds before the next iteration.
    assert asyncio.run(exercise()) < 2.0


def test_idle_worker_waits_without_spinning():
    worker = FakeWorker([])
    runtime = WorkerRuntime(worker, settings(worker_idle_sleep_seconds=0.05))

    async def exercise():
        started = asyncio.get_running_loop().time()
        await runtime.run(max_iterations=2)
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(exercise()) >= 0.1


def test_admin_surface_reports_liveness_and_readiness():
    app = create_admin_app(Settings(), probe=StubProbe())

    with TestClient(app) as client:
        live = client.get("/api/v1/health/live")
        ready = client.get("/api/v1/health/ready")
        assert live.status_code == 200
        assert ready.status_code == 200
        # A worker probe must identify the worker, not the API the web panel reads.
        assert live.json()["service"] == ready.json()["service"] == "nexora-worker"

    with TestClient(create_admin_app(Settings(), probe=StubProbe(redis="down"))) as degraded:
        response = degraded.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json()["dependencies"]["redis"] == "down"


@pytest.mark.parametrize(
    "token,header,status",
    [
        (None, None, 404),
        (TOKEN, None, 401),
        (TOKEN, "Bearer wrong", 401),
        (TOKEN, f"Bearer {TOKEN}", 200),
    ],
)
def test_worker_metrics_use_the_same_gate_as_the_api(token, header, status):
    app = create_admin_app(Settings(metrics_token=token), probe=StubProbe())

    with TestClient(app) as client:
        headers = {"authorization": header} if header else {}
        response = client.get("/metrics", headers=headers)

    assert response.status_code == status
    if status == 200:
        assert "nexora_agent_runs_total" in response.text


def test_worker_admin_surface_exposes_no_tenant_api():
    app = create_admin_app(Settings(), probe=StubProbe())

    with TestClient(app) as client:
        assert client.get("/api/v1/workspaces").status_code == 404
        assert client.get("/docs").status_code == 404


class ClosableAdapter:
    def __init__(self, name="test", failing=False):
        self.name = name
        self.closed = False
        self.failing = failing

    async def aclose(self):
        if self.failing:
            raise RuntimeError("transport already gone")
        self.closed = True


def test_shutdown_releases_adapter_connections():
    adapters = {"a": ClosableAdapter("a"), "b": ClosableAdapter("b")}

    asyncio.run(close_provider_adapters(adapters))

    assert all(adapter.closed for adapter in adapters.values())


def test_a_failing_close_does_not_block_shutdown():
    adapters = {"broken": ClosableAdapter("broken", failing=True), "ok": ClosableAdapter("ok")}

    asyncio.run(close_provider_adapters(adapters))

    assert adapters["ok"].closed


def test_adapters_without_a_close_hook_are_skipped():
    asyncio.run(close_provider_adapters({"plain": object()}))
