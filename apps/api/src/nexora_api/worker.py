import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from nexora_api import metrics
from nexora_api.config import Settings
from nexora_api.logs import context as log_context
from nexora_api.logs import logger
from nexora_api.mcp_gateway import ApprovalRequired, McpGatewayError
from nexora_api.outbox import QUEUE_STREAM, OutboxPublisher
from nexora_api.run_state import ExecutionContext, RunStateStore, WorkerJob
from nexora_api.telemetry import durable_trace_context, record, record_error, span
from nexora_api.tool_repository import ToolGovernanceRepository

WORKER_GROUP = "nexora-agent-workers-v1"
run_log = logger("worker.run")


class RetryableExecutionError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TerminalExecutionError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RunExecutor(Protocol):
    async def execute(
        self,
        context: ExecutionContext,
        is_cancelled: Callable[[], Awaitable[bool]],
    ) -> None: ...


class AgentWorker:
    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        executor: RunExecutor,
        worker_id: str | None = None,
        lease_seconds: int = 30,
    ):
        if not 3 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 3 and 300")
        self.settings = settings
        self.redis = redis
        self.executor = executor
        self.worker_id = worker_id or "worker-" + uuid4().hex
        self.lease_seconds = lease_seconds
        self.state = RunStateStore(settings)
        self.tool_governance = ToolGovernanceRepository(settings)
        self.publisher = OutboxPublisher(settings)

    async def ensure_group(self):
        try:
            await self.redis.xgroup_create(QUEUE_STREAM, WORKER_GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    @staticmethod
    def _field(fields, name):
        value = fields.get(name)
        if value is None:
            value = fields.get(name.encode())
        if isinstance(value, bytes):
            return value.decode()
        return value

    async def _read_message(self, block_ms: int):
        min_idle = self.lease_seconds * 1000
        pending = await self.redis.xpending_range(
            QUEUE_STREAM, WORKER_GROUP, "-", "+", 1, idle=min_idle
        )
        if pending:
            message_id = pending[0]["message_id"]
            claimed = await self.redis.xclaim(
                QUEUE_STREAM,
                WORKER_GROUP,
                self.worker_id,
                min_idle,
                [message_id],
            )
            if claimed:
                return claimed[0]

        messages = await self.redis.xreadgroup(
            WORKER_GROUP,
            self.worker_id,
            {QUEUE_STREAM: ">"},
            count=1,
            block=block_ms,
        )
        if not messages:
            return None
        return messages[0][1][0]

    async def _ack(self, message_id):
        await self.redis.xack(QUEUE_STREAM, WORKER_GROUP, message_id)
        await self.redis.xdel(QUEUE_STREAM, message_id)

    async def _heartbeat(self, context: ExecutionContext, stop: asyncio.Event):
        interval = max(1.0, self.lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                try:
                    renewed = await self.state.renew_lease(
                        context.job_id,
                        context.run_id,
                        self.worker_id,
                        self.lease_seconds,
                    )
                except Exception:
                    return
                if not renewed:
                    return

    async def process_once(self, block_ms: int = 100) -> bool:
        if not 1 <= block_ms <= 5000:
            raise ValueError("block_ms must be between 1 and 5000")
        await self.ensure_group()
        await self.tool_governance.expire_due()
        await self.state.recover_stale()
        published = await self.publisher.publish_batch(self.redis)
        if published:
            metrics.outbox_published_total.inc(published)
        await self._observe_queue_depth()

        item = await self._read_message(block_ms)
        if item is None:
            return False
        message_id, fields = item
        raw = self._field(fields, "job")
        try:
            job = WorkerJob.model_validate_json(raw)
        except (ValidationError, TypeError, ValueError):
            await self._ack(message_id)
            return True

        claim = await self.state.claim(job, self.worker_id, self.lease_seconds)
        if claim.action == "ack" or claim.context is None:
            await self._ack(message_id)
            return True

        context = claim.context
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(context, stop))
        handled = False
        outcome: str | None = "failed_retryable"
        error_code: str | None = None
        started = time.perf_counter()

        async def cancelled():
            return await self.state.is_cancel_requested(context.run_id)

        # The run's durable trace id joins worker, model and tool spans to the API
        # request that created the run, across retries and approval resumes.
        parent = durable_trace_context(
            context.trace_id, context.run_id, self.settings.otel_sample_ratio
        )
        try:
            with span(
                "agent.run",
                context=parent,
                **{
                    "nexora.workspace_id": context.workspace_id,
                    "nexora.run_id": context.run_id,
                    "nexora.agent_id": context.agent_id,
                    "nexora.job_id": context.job_id,
                    "nexora.attempt": context.attempt_count,
                    "nexora.worker_id": self.worker_id,
                },
            ) as active:
                try:
                    await self.executor.execute(context, cancelled)
                except asyncio.CancelledError:
                    # Worker shutdown is not a run outcome and must not skew run metrics.
                    outcome = None
                    raise
                except ApprovalRequired as exc:
                    outcome = "waiting_for_approval"
                    record(active, **{"nexora.approval_id": exc.approval_id})
                    handled = await self.state.wait_for_approval(
                        job,
                        self.worker_id,
                        exc.tool_call_id,
                        exc.approval_id,
                    )
                except McpGatewayError as exc:
                    outcome = "failed_retryable" if exc.retryable else "failed_terminal"
                    error_code = exc.code
                    record_error(active, exc.code)
                    handled = await self.state.complete_failure(
                        job, self.worker_id, exc.code, retryable=exc.retryable
                    )
                except RetryableExecutionError as exc:
                    error_code = exc.code
                    record_error(active, exc.code)
                    handled = await self.state.complete_failure(
                        job, self.worker_id, exc.code, retryable=True
                    )
                except TerminalExecutionError as exc:
                    outcome = "cancelled" if exc.code == "cancel_requested" else "failed_terminal"
                    error_code = exc.code
                    record_error(active, exc.code)
                    handled = await self.state.complete_failure(
                        job, self.worker_id, exc.code, retryable=False
                    )
                except Exception:
                    error_code = "executor_error"
                    record_error(active, "executor_error")
                    handled = await self.state.complete_failure(
                        job, self.worker_id, "executor_error", retryable=True
                    )
                else:
                    outcome = "succeeded"
                    handled = await self.state.complete_success(job, self.worker_id)
                record(active, **{"nexora.outcome": outcome})
        finally:
            stop.set()
            await heartbeat
            if outcome is not None:
                metrics.observe_run(outcome, time.perf_counter() - started)
                run_log.info(
                    "run attempt finished",
                    extra=log_context(
                        workspace_id=context.workspace_id,
                        run_id=context.run_id,
                        agent_id=context.agent_id,
                        attempt=context.attempt_count,
                        worker_id=self.worker_id,
                        outcome=outcome,
                        error_code=error_code,
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    ),
                )

        if handled:
            await self._ack(message_id)
        return True

    async def _observe_queue_depth(self) -> None:
        try:
            depth = await self.redis.xlen(QUEUE_STREAM)
        except Exception:
            return
        metrics.queue_depth.set(depth)
