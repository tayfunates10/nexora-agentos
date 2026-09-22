"""Worker process entrypoint.

Run with ``python -m nexora_api.worker_main``. The process serves an admin surface for
liveness, readiness and guarded metric scraping alongside the job loop, because a
deployed worker needs probes and its own exposition; the API process cannot report on
it. SIGTERM stops the loop between jobs so an in-flight attempt finishes under its own
lease instead of being recovered by another worker.
"""

import asyncio
import contextlib
import signal
import sys

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from nexora_api import metrics
from nexora_api.config import Settings
from nexora_api.health import DependencyProbe, HealthResponse
from nexora_api.integration_mcp import IntegrationMcpAdapter
from nexora_api.logs import configure_logging, logger
from nexora_api.logs import context as log_context
from nexora_api.runtime_config import RuntimeConfigError
from nexora_api.telemetry import configure_telemetry
from nexora_api.worker_service import (
    WorkerRuntime,
    build_agent_update_scheduler,
    build_evaluation_judge_worker,
    build_knowledge_worker,
    build_mcp_adapters,
    build_provider_adapters,
    build_retriever,
    build_spend_alert_notifier,
    build_worker,
    close_mcp_adapters,
    close_provider_adapters,
    close_retriever,
    close_spend_alert_notifier,
    load_worker_config,
)

SERVICE = "nexora-worker"
main_log = logger("worker.main")


def create_admin_app(settings: Settings, probe=None) -> FastAPI:
    """Probe and scrape surface for the worker process. It exposes no tenant data."""
    app = FastAPI(title="Nexora AgentOS Worker", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.probe = probe

    def envelope(status: int, code: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"error": {"code": code}})

    @app.get("/api/v1/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(status="ok", service=SERVICE)

    @app.get(
        "/api/v1/health/ready",
        response_model=HealthResponse,
        responses={503: {"model": HealthResponse}},
    )
    async def ready(response: Response) -> HealthResponse:
        dependencies = await app.state.probe.check()
        healthy = dependencies.postgres == dependencies.redis == "up"
        response.status_code = 200 if healthy else 503
        return HealthResponse(
            status="ok" if healthy else "degraded",
            service=SERVICE,
            dependencies=dependencies,
        )

    @app.get("/metrics", include_in_schema=False)
    async def scrape(request: Request):
        decision = metrics.authorize_scrape(
            request.headers.get("authorization", ""), settings.metrics_token
        )
        if decision == "disabled":
            return envelope(404, "not_found")
        if decision == "unauthorized":
            response = envelope(401, "unauthorized")
            response.headers["www-authenticate"] = "Bearer"
            return response
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    return app


async def serve(settings: Settings) -> None:
    config = load_worker_config(settings)
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_timeout=settings.dependency_timeout_seconds,
        socket_connect_timeout=settings.dependency_timeout_seconds,
    )
    adapters = build_provider_adapters(settings, config)
    mcp_adapters = build_mcp_adapters(config)
    mcp_adapters["nexora-integrations"] = IntegrationMcpAdapter(settings)
    retriever = build_retriever(settings, config)
    worker = build_worker(
        settings,
        config,
        redis,
        adapters=adapters,
        mcp_adapters=mcp_adapters,
        retriever=retriever,
    )
    knowledge_worker = build_knowledge_worker(
        settings,
        retriever,
        worker_id=worker.worker_id,
    )
    evaluation_judge_worker = build_evaluation_judge_worker(
        settings,
        config,
        adapters,
        worker_id=worker.worker_id,
    )
    spend_alert_notifier = build_spend_alert_notifier(
        settings,
        config,
        worker_id=worker.worker_id,
    )
    agent_update_scheduler = build_agent_update_scheduler(settings)
    runtime = WorkerRuntime(
        worker,
        settings,
        knowledge_worker=knowledge_worker,
        evaluation_judge_worker=evaluation_judge_worker,
        spend_alert_notifier=spend_alert_notifier,
        agent_update_scheduler=agent_update_scheduler,
    )
    admin = create_admin_app(settings, DependencyProbe(settings, redis))
    server = uvicorn.Server(
        uvicorn.Config(
            admin,
            host="0.0.0.0",
            port=settings.worker_admin_port,
            log_config=None,
            access_log=False,
        )
    )

    loop = asyncio.get_running_loop()
    for received in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(received, runtime.request_stop)

    admin_task = asyncio.create_task(server.serve())
    main_log.info(
        "worker process ready",
        extra=log_context(worker_id=worker.worker_id, outcome="ready"),
    )
    try:
        await asyncio.wait_for(runtime.run(), timeout=None)
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(admin_task, timeout=settings.worker_shutdown_grace_seconds)
        await close_retriever(retriever)
        await close_spend_alert_notifier(spend_alert_notifier)
        await close_mcp_adapters(mcp_adapters)
        await close_provider_adapters(adapters)
        await redis.aclose()


def main() -> int:
    settings = Settings()
    configure_logging(settings)
    configure_telemetry(settings)
    try:
        asyncio.run(serve(settings))
    except RuntimeConfigError as exc:
        # Fail closed and say why: a worker without operator profiles must not run.
        main_log.error("worker configuration rejected", extra=log_context(error_code=str(exc)))
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
