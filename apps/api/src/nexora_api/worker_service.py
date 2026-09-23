"""Worker process runtime: build the executor from operator configuration and run it.

The loop owns process lifetime concerns only. Claiming, leasing, fencing, retries,
approval suspension and cancellation stay in AgentWorker; model turns, tool calls and
their durability stay in DurableAgentExecutor. A shutdown request stops the loop between
jobs so an in-flight attempt keeps its lease and finishes, rather than being abandoned
for another worker to recover.
"""

import asyncio
import contextlib
import os
import time

import httpx
from redis.asyncio import Redis

from nexora_api.answer_cache import SemanticAnswerCache, SemanticAnswerCacheConfig
from nexora_api.browser_mcp import BROWSER_SERVER_KEY, BrowserMcpAdapter
from nexora_api.config import Settings
from nexora_api.connector_mcp import CONNECTOR_SERVER_KEY, ConnectorMcpAdapter
from nexora_api.embeddings import OpenAIEmbeddingsAdapter
from nexora_api.evaluation_judge_worker import EvaluationJudgeWorker
from nexora_api.executor import DurableAgentExecutor
from nexora_api.executor_store import ExecutorStore
from nexora_api.knowledge_worker import KnowledgeIngestionWorker
from nexora_api.logs import context as log_context
from nexora_api.logs import logger
from nexora_api.mcp_gateway import McpGateway, McpToolAdapter
from nexora_api.mcp_http import McpHttpEndpoint, StreamableHttpMcpAdapter
from nexora_api.model_routing import ModelRouter, ProviderAdapter
from nexora_api.openai_responses import OpenAIResponsesAdapter
from nexora_api.rag_pipeline import RagEmbeddingPipeline
from nexora_api.rag_repository import RagRepository
from nexora_api.runtime_config import RuntimeConfig, RuntimeConfigError, load_runtime_config
from nexora_api.spend_alert_worker import SpendAlertNotifier, SpendAlertWebhook
from nexora_api.task_mcp import TASK_SERVER_KEY, TaskMcpAdapter
from nexora_api.tenant_agent_repository import TenantAgentRepository
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


def build_mcp_adapters(
    config: RuntimeConfig,
    clients: dict[str, httpx.AsyncClient] | None = None,
) -> dict[str, McpToolAdapter]:
    """Build only operator-declared remote MCP transports.

    URLs and credential environment-variable names come from the mounted runtime
    configuration; workspace tool registrations can select only their bounded server_key.
    """
    adapters: dict[str, McpToolAdapter] = {}
    for server_key, server in sorted(config.mcp_servers.items()):
        token = None
        if server.bearer_token_env:
            token = os.getenv(server.bearer_token_env)
            if not token:
                raise RuntimeConfigError(
                    f"{server.bearer_token_env} is required by MCP server {server_key}"
                )
        try:
            endpoint = McpHttpEndpoint(
                url=server.url,
                bearer_token=token,
                timeout_seconds=server.timeout_seconds,
                max_response_bytes=server.max_response_bytes,
            )
        except ValueError as exc:
            raise RuntimeConfigError(f"invalid MCP server {server_key}: {exc}") from exc
        adapters[server_key] = StreamableHttpMcpAdapter(
            endpoint,
            client=(clients or {}).get(server_key),
        )
    return adapters


def build_retriever(
    settings: Settings,
    config: RuntimeConfig,
    client: httpx.AsyncClient | None = None,
) -> RagEmbeddingPipeline | None:
    """Build retrieval only when an operator explicitly enables it."""
    retrieval = config.retrieval
    if retrieval is None:
        return None
    if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
        raise RuntimeConfigError("NEXORA_OPENAI_API_KEY is required by configured retrieval")
    adapter = OpenAIEmbeddingsAdapter(
        api_key=settings.openai_api_key,
        model_dimensions={retrieval.model: frozenset({retrieval.dimensions})},
        client=client,
    )
    return RagEmbeddingPipeline(
        RagRepository(settings),
        adapter,
        embedding_model=retrieval.model,
        dimensions=retrieval.dimensions,
        batch_size=retrieval.batch_size,
        timeout_seconds=retrieval.timeout_seconds,
        retrieval_limit=retrieval.limit,
        retrieval_strategy=retrieval.strategy,
        ann_enabled=retrieval.ann is not None,
        hnsw_ef_search=retrieval.ann.ef_search if retrieval.ann is not None else 100,
        spend=config.embedding_spend(),
    )


def build_semantic_answer_cache(
    settings: Settings,
    config: RuntimeConfig,
    client: httpx.AsyncClient | None = None,
) -> SemanticAnswerCache | None:
    """Build semantic matching only when the operator explicitly configures it."""
    semantic = config.answer_cache_semantic
    if semantic is None:
        return None
    if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
        raise RuntimeConfigError(
            "NEXORA_OPENAI_API_KEY is required by configured semantic answer cache"
        )
    adapter = OpenAIEmbeddingsAdapter(
        api_key=settings.openai_api_key,
        model_dimensions={semantic.model: frozenset({semantic.dimensions})},
        client=client,
    )
    return SemanticAnswerCache(
        adapter,
        SemanticAnswerCacheConfig(
            model=semantic.model,
            dimensions=semantic.dimensions,
            similarity_threshold=semantic.similarity_threshold,
            timeout_seconds=semantic.timeout_seconds,
            spend=config.semantic_cache_spend(),
        ),
    )


def build_knowledge_worker(
    settings: Settings,
    retriever: RagEmbeddingPipeline | None,
    *,
    worker_id: str,
) -> KnowledgeIngestionWorker | None:
    if retriever is None:
        return None
    return KnowledgeIngestionWorker(
        settings,
        retriever,
        worker_id=worker_id,
        lease_seconds=settings.worker_lease_seconds,
    )


def build_evaluation_judge_worker(
    settings: Settings,
    config: RuntimeConfig,
    adapters: dict[str, ProviderAdapter],
    *,
    worker_id: str,
) -> EvaluationJudgeWorker | None:
    judge = config.evaluation_judge
    if judge is None:
        return None
    adapter = adapters.get(judge.provider)
    if adapter is None:
        raise RuntimeConfigError("evaluation judge provider adapter is unavailable")
    return EvaluationJudgeWorker(
        settings,
        adapter,
        judge,
        worker_id=worker_id,
        lease_seconds=settings.worker_lease_seconds,
        spend=config.spend_policy(),
    )


def build_spend_alert_notifier(
    settings: Settings,
    config: RuntimeConfig,
    *,
    worker_id: str,
    client: httpx.AsyncClient | None = None,
) -> SpendAlertNotifier | None:
    """Deliver budget alerts only when an operator declares where they go."""
    webhook = config.spend_alert_webhook
    if webhook is None:
        return None
    secret = os.getenv(webhook.signing_secret_env)
    if not secret:
        raise RuntimeConfigError(
            f"{webhook.signing_secret_env} is required by the spend alert webhook"
        )
    token = None
    if webhook.bearer_token_env:
        token = os.getenv(webhook.bearer_token_env)
        if not token:
            raise RuntimeConfigError(
                f"{webhook.bearer_token_env} is required by the spend alert webhook"
            )
    try:
        endpoint = SpendAlertWebhook(
            url=webhook.url,
            signing_secret=secret,
            bearer_token=token,
            timeout_seconds=webhook.timeout_seconds,
        )
    except ValueError as exc:
        raise RuntimeConfigError(f"invalid spend alert webhook: {exc}") from exc
    return SpendAlertNotifier(
        settings,
        endpoint,
        worker_id=worker_id,
        lease_seconds=settings.worker_lease_seconds,
        client=client,
    )


class TenantAgentUpdateScheduler:
    """Periodically advances instances that explicitly opted into automatic updates."""

    def __init__(
        self,
        repository: TenantAgentRepository,
        *,
        interval_seconds: float = 60.0,
        batch_size: int = 50,
        clock=time.monotonic,
    ):
        if not 1.0 <= interval_seconds <= 3600.0:
            raise ValueError("interval_seconds must be between 1 and 3600")
        if not 1 <= batch_size <= 200:
            raise ValueError("batch_size must be between 1 and 200")
        self.repository = repository
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.clock = clock
        self.cursor = None
        self._next_run = 0.0

    async def process_once(self) -> bool:
        now = self.clock()
        if now < self._next_run:
            return False
        updated, cursor = await self.repository.apply_automatic_updates(
            limit=self.batch_size,
            cursor=self.cursor,
        )
        self.cursor = cursor
        # Drain the next page without an artificial minute between batches. Once a full
        # sweep ends, wait before scanning from the beginning again.
        self._next_run = now if cursor is not None else now + self.interval_seconds
        return updated > 0 or cursor is not None


def build_agent_update_scheduler(settings: Settings) -> TenantAgentUpdateScheduler:
    return TenantAgentUpdateScheduler(TenantAgentRepository(settings))


def build_worker(
    settings: Settings,
    config: RuntimeConfig,
    redis: Redis,
    *,
    adapters: dict[str, ProviderAdapter] | None = None,
    mcp_adapters: dict[str, McpToolAdapter] | None = None,
    retriever: RagEmbeddingPipeline | None = None,
    semantic_cache: SemanticAnswerCache | None = None,
    worker_id: str | None = None,
) -> AgentWorker:
    resolved_mcp_adapters = dict(
        mcp_adapters if mcp_adapters is not None else build_mcp_adapters(config)
    )
    # "connector" is a built-in, context-aware transport. It is never supplied by a
    # tenant and cannot be replaced by an operator URL.
    resolved_mcp_adapters[CONNECTOR_SERVER_KEY] = ConnectorMcpAdapter(settings)
    resolved_mcp_adapters[TASK_SERVER_KEY] = TaskMcpAdapter(settings)
    if settings.browser_runtime_url is not None and settings.browser_runtime_token is not None:
        resolved_mcp_adapters[BROWSER_SERVER_KEY] = BrowserMcpAdapter(settings)
    executor = DurableAgentExecutor(
        store=ExecutorStore(settings, spend=config.spend_policy()),
        router=ModelRouter(config.candidates()),
        adapters=adapters if adapters is not None else build_provider_adapters(settings, config),
        gateway=McpGateway(settings, adapters=resolved_mcp_adapters),
        profiles=config.execution_profiles(),
        retriever=retriever,
        semantic_cache=semantic_cache,
    )
    return AgentWorker(
        settings,
        redis,
        executor,
        worker_id=worker_id,
        lease_seconds=settings.worker_lease_seconds,
    )


async def close_mcp_adapters(adapters: dict[str, McpToolAdapter]) -> None:
    for server_key, adapter in adapters.items():
        closer = getattr(adapter, "aclose", None)
        if closer is None:
            continue
        try:
            await closer()
        except Exception:
            service_log.warning(
                "MCP adapter close failed",
                extra=log_context(server_key=server_key, outcome="ignored"),
            )


async def close_semantic_answer_cache(cache: SemanticAnswerCache | None) -> None:
    if cache is None:
        return
    try:
        await cache.aclose()
    except Exception:
        service_log.warning(
            "semantic answer cache close failed",
            extra=log_context(outcome="ignored"),
        )


async def close_retriever(retriever: RagEmbeddingPipeline | None) -> None:
    if retriever is None:
        return
    try:
        await retriever.aclose()
    except Exception:
        service_log.warning(
            "retriever close failed",
            extra=log_context(outcome="ignored"),
        )


async def close_spend_alert_notifier(notifier: SpendAlertNotifier | None) -> None:
    if notifier is None:
        return
    try:
        await notifier.aclose()
    except Exception:
        service_log.warning(
            "spend alert notifier close failed",
            extra=log_context(outcome="ignored"),
        )


async def close_provider_adapters(adapters: dict[str, ProviderAdapter]) -> None:
    """Release adapter-owned connections on shutdown; a closing failure must not block exit."""
    for adapter in adapters.values():
        closer = getattr(adapter, "aclose", None)
        if closer is None:
            continue
        try:
            await closer()
        except Exception:
            service_log.warning(
                "adapter close failed",
                extra=log_context(provider=getattr(adapter, "name", None), outcome="ignored"),
            )


class WorkerRuntime:
    """Runs one AgentWorker until stopped, with idle and failure backoff."""

    def __init__(
        self,
        worker: AgentWorker,
        settings: Settings,
        knowledge_worker: KnowledgeIngestionWorker | None = None,
        evaluation_judge_worker: EvaluationJudgeWorker | None = None,
        spend_alert_notifier: SpendAlertNotifier | None = None,
        agent_update_scheduler: TenantAgentUpdateScheduler | None = None,
        backoff_base_seconds: float = 2.0,
    ):
        if not 0 < backoff_base_seconds <= 60:
            raise ValueError("backoff_base_seconds must be between 0 and 60")
        self.worker = worker
        self.knowledge_worker = knowledge_worker
        self.evaluation_judge_worker = evaluation_judge_worker
        self.spend_alert_notifier = spend_alert_notifier
        self.agent_update_scheduler = agent_update_scheduler
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
                if self.knowledge_worker is not None:
                    knowledge_handled = await self.knowledge_worker.process_once()
                    handled = knowledge_handled or handled
                if self.evaluation_judge_worker is not None:
                    judge_handled = await self.evaluation_judge_worker.process_once()
                    handled = judge_handled or handled
                if self.spend_alert_notifier is not None:
                    alert_handled = await self.spend_alert_notifier.process_once()
                    handled = alert_handled or handled
                if self.agent_update_scheduler is not None:
                    update_handled = await self.agent_update_scheduler.process_once()
                    handled = update_handled or handled
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
