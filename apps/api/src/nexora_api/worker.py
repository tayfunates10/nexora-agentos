import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from nexora_api.config import Settings
from nexora_api.mcp_gateway import ApprovalRequired, McpGatewayError
from nexora_api.outbox import QUEUE_STREAM, OutboxPublisher
from nexora_api.run_state import ExecutionContext, RunStateStore, WorkerJob
from nexora_api.tool_repository import ToolGovernanceRepository

WORKER_GROUP = "nexora-agent-workers-v1"


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

    async def _heartbeat(
        self,
        context: ExecutionContext,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ):
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
                    lease_lost.set()
                    return
                if not renewed:
                    lease_lost.set()
                    return

    async def process_once(self, block_ms: int = 100) -> bool:
        if not 1 <= block_ms <= 5000:
            raise ValueError("block_ms must be between 1 and 5000")
        await self.ensure_group()
        await self.tool_governance.expire_due()
        await self.state.recover_stale()
        await self.publisher.publish_batch(self.redis)

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
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(context, stop, lease_lost))
        handled = False

        async def cancelled():
            if lease_lost.is_set():
                return True
            return await self.state.is_cancel_requested(context.run_id)

        try:
            await self.executor.execute(context, cancelled)
        except asyncio.CancelledError:
            raise
        except ApprovalRequired as exc:
            handled = await self.state.wait_for_approval(
                job,
                self.worker_id,
                exc.tool_call_id,
                exc.approval_id,
            )
        except McpGatewayError as exc:
            handled = await self.state.complete_failure(
                job, self.worker_id, exc.code, retryable=exc.retryable
            )
        except RetryableExecutionError as exc:
            handled = await self.state.complete_failure(
                job, self.worker_id, exc.code, retryable=True
            )
        except TerminalExecutionError as exc:
            handled = await self.state.complete_failure(
                job, self.worker_id, exc.code, retryable=False
            )
        except Exception:
            handled = await self.state.complete_failure(
                job, self.worker_id, "executor_error", retryable=True
            )
        else:
            handled = await self.state.complete_success(job, self.worker_id)
        finally:
            stop.set()
            await heartbeat

        if handled:
            await self._ack(message_id)
        return True
