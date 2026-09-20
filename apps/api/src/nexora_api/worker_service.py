"""Worker process runtime: build the executor from operator configuration and run it.

The loop owns process lifetime concerns only. Claiming, leasing, fencing, retries,
approval suspension and cancellation stay in AgentWorker; model turns, tool calls and
their durability stay in DurableAgentExecutor. A shutdown request stops the loop between
jobs so an in-flight attempt keeps its lease and finishes, rather than being abandoned
for another worker to recover.
"""

import asyncio
import contextlib
import time

import httpx
from redis.asyncio import Redis

from nexora_api.config import Settings
from nexora_api.executor import DurableAgentExecutor
from nexora_api.executor_store import ExecutorStore
from nexora_api.logs import context as log_context
from nexora_api.logs import logger
from nexora_api.mcp_gateway import McpGateway, McpToolAdapter
from nexora_api.model_routing import ModelRouter, ProviderAdapter
from nexora_api.openai_responses import OpenAIResponsesAdapter
from nexora_api.runtime_config import RuntimeConfig, RuntimeConfigError, load_runtime_config
from nexora_api.worker import AgentWorker

MAX_ERROR_BACKOFF_SECONDS = 30.0
service_log = logger("worker.service")


def load_worker_config(settings: Settings) -> RuntimeConfig:
    """No configured profile means no execution, so an unset path stops the worker."""
    if not settings.worker_runtime_config:
        raise RuntimeConfigError("NEXORA_WORKER_RUNTIME_CONFIG must point at a runtime config")
    return load_runtime_config(settings.worker_runtime_config)


def build_provider_adapters(
    settings: Settings,
    config: RuntimeConfig,
    client: httpx.AsyncClient | None = None,
) -> dict[str, ProviderAdapter]:
    """Construct the adapters the configuration declares. Credentials come from env only."""
    adapters: dict[str, ProviderAdapter] = {}
    for provider in sorted(config.providers):
        if provider == "openai":
            if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
                raise RuntimeConfigError("NEXORA_OPENAI_API_KEY is required by the openai provider")
            adapters[provider] = OpenAIResponsesAdapter(
                api_key=settings.openai_api_key,
                model_capabilities=config.model_capabilities(provider),
                client=client,
            )
        else:
            raise RuntimeConfigError(f"no adapter is available for provider: {provider}")
    return adapters


def build_worker(
    settings: Settings,
    config: RuntimeConfig,
    redis: Redis,
    *,
    adapters: dict[str, ProviderAdapter] | None = None,
    mcp_adapters: dict[str, McpToolAdapter] | None = None,
    worker_id: str | None = None,
) -> AgentWorker:
    executor = DurableAgentExecutor(
        store=ExecutorStore(settings),
        router=ModelRouter(config.candidates()),
        adapters=adapters if adapters is not None else build_provider_adapters(settings, config),
        gateway=McpGateway(settings, adapters=mcp_adapters),
        profiles=config.execution_profiles(),
    )
    return AgentWorker(
        settings,
        redis,
        executor,
        worker_id=worker_id,
        lease_seconds=settings.worker_lease_seconds,
    )


class WorkerRuntime:
    """Runs one AgentWorker until stopped, with idle and failure backoff."""

    def __init__(
        self,
        worker: AgentWorker,
        settings: Settings,
        backoff_base_seconds: float = 2.0,
    ):
        if not 0 < backoff_base_seconds <= 60:
            raise ValueError("backoff_base_seconds must be between 0 and 60")
        self.worker = worker
        self.settings = settings
        self._stop = asyncio.Event()
        self._idle = settings.worker_idle_sleep_seconds
        self._backoff_base = backoff_base_seconds

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    async def _pause(self, seconds: float) -> None:
        # Waiting on the stop event keeps shutdown responsive during backoff.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def run(self, max_iterations: int | None = None) -> int:
        """Process jobs until stopped. Returns the number of completed iterations."""
        iterations = 0
        failures = 0
        service_log.info(
            "worker started",
            extra=log_context(worker_id=self.worker.worker_id),
        )
        while not self._stop.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break
            started = time.perf_counter()
            try:
                handled = await self.worker.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A worker must outlive a dependency blip; back off instead of exiting.
                failures += 1
                delay = min(MAX_ERROR_BACKOFF_SECONDS, self._backoff_base ** min(failures, 5))
                service_log.warning(
                    "worker iteration failed",
                    extra=log_context(
                        worker_id=self.worker.worker_id,
                        error_code=type(exc).__name__,
                        outcome="backoff",
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    ),
                )
                await self._pause(delay)
            else:
                failures = 0
                if not handled:
                    await self._pause(self._idle)
            iterations += 1
        service_log.info(
            "worker stopped",
            extra=log_context(worker_id=self.worker.worker_id, outcome="shutdown"),
        )
        return iterations
