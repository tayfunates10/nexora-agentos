import re
import time
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind
from redis.asyncio import Redis
from starlette.exceptions import HTTPException

from nexora_api import metrics
from nexora_api.agent_repository import AgentRuntimeRepository
from nexora_api.agents import router as agent_router
from nexora_api.config import Settings
from nexora_api.evaluation_judge_repository import EvaluationJudgeRepository
from nexora_api.evaluation_judges import router as evaluation_judge_router
from nexora_api.evaluation_repository import EvaluationRepository
from nexora_api.evaluations import router as evaluation_router
from nexora_api.health import DependencyProbe, HealthResponse, Probe
from nexora_api.knowledge import router as knowledge_router
from nexora_api.logs import configure_logging, context, logger
from nexora_api.mcp_gateway import McpGateway
from nexora_api.rag_repository import RagRepository
from nexora_api.spend import router as spend_router
from nexora_api.spend_repository import SpendRepository
from nexora_api.telemetry import configure_telemetry, record, record_error, span
from nexora_api.tooling import router as tool_router
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import router as workspace_router

METRICS_PATH = "/metrics"
access_log = logger("api.access")


def get_probe(request: Request) -> Probe:
    return request.app.state.probe


def create_app(settings: Settings | None = None, probe: Probe | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings)
    configure_telemetry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_timeout=settings.dependency_timeout_seconds,
            socket_connect_timeout=settings.dependency_timeout_seconds,
        )
        app.state.probe = probe or DependencyProbe(settings, redis)
        app.state.settings = settings
        app.state.workspaces = WorkspaceRepository(settings)
        app.state.agent_runtime = AgentRuntimeRepository(settings)
        app.state.evaluations = EvaluationRepository(settings)
        app.state.eval_judges = EvaluationJudgeRepository(settings)
        app.state.mcp_gateway = McpGateway(settings)
        app.state.rag = RagRepository(settings)
        app.state.spend = SpendRepository(settings)
        app.state.tool_governance = app.state.mcp_gateway.repository
        try:
            yield
        finally:
            await redis.aclose()

    app = FastAPI(title="Nexora AgentOS API", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def observed_request(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request.state.request_id = (
            supplied if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", supplied) else str(uuid4())
        )
        # Inbound trace context is accepted for correlation only; it never grants access.
        parent = extract(dict(request.headers))
        started = time.perf_counter()
        with span(
            f"HTTP {request.method}",
            context=parent,
            kind=SpanKind.SERVER,
            **{
                "http.request.method": request.method,
                "nexora.request_id": request.state.request_id,
            },
        ) as active:
            try:
                response = await call_next(request)
            except Exception:
                response = error(request, 500, "internal_error", "An unexpected error occurred")
            elapsed = time.perf_counter() - started
            route = getattr(request.scope.get("route"), "path", None)
            response.headers["x-request-id"] = request.state.request_id
            record(
                active,
                **{
                    "http.response.status_code": response.status_code,
                    "http.route": route or "unmatched",
                },
            )
            if response.status_code >= 500:
                record_error(active, f"http_{response.status_code}")
            if route != METRICS_PATH:
                metrics.observe_http(request.method, route, response.status_code, elapsed)
                access_log.info(
                    "request",
                    extra=context(
                        method=request.method,
                        route=metrics.route_label(route),
                        status=response.status_code,
                        duration_ms=round(elapsed * 1000, 3),
                        request_id=request.state.request_id,
                    ),
                )
            return response

    def error(request: Request, status: int, code: str, message: str):
        return JSONResponse(
            status_code=status,
            content={
                "error": {"code": code, "message": message, "request_id": request.state.request_id}
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        response = error(request, exc.status_code, f"http_{exc.status_code}", "Request failed")
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error(request, 422, "validation_error", "Request validation failed")

    @app.get("/api/v1/health/live", response_model=HealthResponse, tags=["health"])
    async def live() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get(
        "/api/v1/health/ready",
        response_model=HealthResponse,
        tags=["health"],
        responses={503: {"model": HealthResponse}},
    )
    async def ready(response: Response, checker: Annotated[Probe, Depends(get_probe)]):
        dependencies = await checker.check()
        healthy = dependencies.postgres == dependencies.redis == "up"
        response.status_code = 200 if healthy else 503
        return HealthResponse(status="ok" if healthy else "degraded", dependencies=dependencies)

    @app.get(METRICS_PATH, include_in_schema=False)
    async def scrape(request: Request):
        # Operational data is not public: no token means the endpoint does not exist.
        decision = metrics.authorize_scrape(
            request.headers.get("authorization", ""), settings.metrics_token
        )
        if decision == "disabled":
            return error(request, 404, "not_found", "Request failed")
        if decision == "unauthorized":
            response = error(request, 401, "unauthorized", "Request failed")
            response.headers["www-authenticate"] = "Bearer"
            return response
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    app.include_router(workspace_router)
    app.include_router(agent_router)
    app.include_router(evaluation_router)
    app.include_router(evaluation_judge_router)
    app.include_router(tool_router)
    app.include_router(knowledge_router)
    app.include_router(spend_router)
    return app


app = create_app()
