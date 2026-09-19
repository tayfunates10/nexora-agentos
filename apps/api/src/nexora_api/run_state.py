import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from nexora_api.config import Settings
from nexora_api.runtime_events import append_run_event

MAX_RUN_ATTEMPTS = 3


class JobPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    workspace_id: UUID
    agent_id: UUID
    trace_id: UUID


class WorkerJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    workspace_id: UUID
    run_id: UUID
    topic: Literal["agent.run.queued.v1"]
    payload: JobPayload


@dataclass(frozen=True)
class ExecutionContext:
    job_id: UUID
    worker_id: str
    workspace_id: UUID
    run_id: UUID
    agent_id: UUID
    trace_id: UUID
    input_text: str
    instructions: str
    model_profile: str
    attempt_count: int


@dataclass(frozen=True)
class ClaimResult:
    action: Literal["execute", "ack"]
    context: ExecutionContext | None = None


def retry_delay_seconds(run_id: UUID, attempt_count: int) -> float:
    base = min(60.0, float(2 ** min(attempt_count, 6)))
    digest = hashlib.sha256(f"{run_id}:{attempt_count}".encode()).digest()
    jitter = int.from_bytes(digest[:2], "big") / 65535
    return round(base * (1 + 0.25 * jitter), 3)


class RunStateStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ) as connection:
            yield connection

    async def _validate_job(self, connection, job: WorkerJob):
        result = await connection.execute(
            """SELECT id,workspace_id,run_id,topic,payload,dead_lettered_at
               FROM job_outbox WHERE id=%s FOR UPDATE""",
            (job.job_id,),
        )
        row = await result.fetchone()
        if not row or row["dead_lettered_at"] is not None:
            return None
        durable_payload = JobPayload.model_validate(row["payload"])
        if (
            row["workspace_id"] != job.workspace_id
            or row["run_id"] != job.run_id
            or row["topic"] != job.topic
            or durable_payload != job.payload
            or job.payload.workspace_id != job.workspace_id
            or job.payload.run_id != job.run_id
        ):
            return None
        return durable_payload

    async def claim(self, job: WorkerJob, worker_id: str, lease_seconds: int = 30) -> ClaimResult:
        if not 3 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 3 and 300")
        async with self.connection() as connection:
            durable_payload = await self._validate_job(connection, job)
            if durable_payload is None:
                return ClaimResult("ack")

            receipt_result = await connection.execute(
                "SELECT * FROM worker_job_receipts WHERE job_id=%s FOR UPDATE",
                (job.job_id,),
            )
            receipt = await receipt_result.fetchone()
            if receipt and receipt["status"] != "processing":
                return ClaimResult("ack")

            run_result = await connection.execute(
                """SELECT r.*,a.instructions,a.model_profile
                   FROM agent_runs r
                   JOIN agent_definitions a
                     ON a.id=r.agent_id AND a.workspace_id=r.workspace_id
                   WHERE r.id=%s AND r.workspace_id=%s
                   FOR UPDATE OF r""",
                (job.run_id, job.workspace_id),
            )
            run = await run_result.fetchone()
            if not run:
                return ClaimResult("ack")
            if (
                run["agent_id"] != durable_payload.agent_id
                or run["trace_id"] != durable_payload.trace_id
            ):
                return ClaimResult("ack")

            if run["status"] in ("succeeded", "failed", "cancelled"):
                terminal = "cancelled" if run["status"] == "cancelled" else run["status"]
                await self._terminal_receipt(connection, job, terminal, worker_id)
                return ClaimResult("ack")
            if run["status"] == "waiting_for_approval":
                await self._terminal_receipt(connection, job, "superseded", worker_id)
                return ClaimResult("ack")

            now_result = await connection.execute("SELECT now() AS now")
            now = (await now_result.fetchone())["now"]
            if receipt and receipt["lease_expires_at"] and receipt["lease_expires_at"] > now:
                return ClaimResult("ack")

            if run["status"] == "running":
                if run["lease_expires_at"] and run["lease_expires_at"] > now:
                    await self._terminal_receipt(connection, job, "superseded", worker_id)
                    return ClaimResult("ack")
                await connection.execute(
                    """UPDATE worker_job_receipts
                       SET status='superseded',updated_at=now(),last_error_code='lease_expired'
                       WHERE run_id=%s AND status='processing'""",
                    (run["id"],),
                )
                await connection.execute(
                    """UPDATE agent_runs
                       SET status='queued',lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                       WHERE id=%s""",
                    (run["id"],),
                )
                await append_run_event(
                    connection,
                    run["workspace_id"],
                    run["id"],
                    "run.recovered",
                    {"previous_attempt": run["attempt_count"]},
                )

            if run["cancel_requested_at"] is not None:
                await self._cancel_locked(connection, run, job, worker_id)
                return ClaimResult("ack")

            updated = await connection.execute(
                """UPDATE agent_runs
                   SET status='running',attempt_count=attempt_count+1,lease_owner=%s,
                       lease_expires_at=now()+(%s * interval '1 second'),updated_at=now(),
                       failure_code=NULL
                   WHERE id=%s
                   RETURNING attempt_count,lease_expires_at""",
                (worker_id, lease_seconds, run["id"]),
            )
            state = await updated.fetchone()
            await connection.execute(
                """INSERT INTO worker_job_receipts
                   (job_id,workspace_id,run_id,status,delivery_count,worker_id,lease_expires_at)
                   VALUES (%s,%s,%s,'processing',1,%s,%s)
                   ON CONFLICT (job_id) DO UPDATE SET
                     status='processing',
                     delivery_count=worker_job_receipts.delivery_count+1,
                     worker_id=EXCLUDED.worker_id,
                     lease_expires_at=EXCLUDED.lease_expires_at,
                     last_error_code=NULL,
                     updated_at=now()""",
                (
                    job.job_id,
                    job.workspace_id,
                    job.run_id,
                    worker_id,
                    state["lease_expires_at"],
                ),
            )
            await append_run_event(
                connection,
                run["workspace_id"],
                run["id"],
                "run.started",
                {"attempt": state["attempt_count"], "job_id": str(job.job_id)},
            )
            context = ExecutionContext(
                job_id=job.job_id,
                worker_id=worker_id,
                workspace_id=run["workspace_id"],
                run_id=run["id"],
                agent_id=run["agent_id"],
                trace_id=run["trace_id"],
                input_text=run["input_text"],
                instructions=run["instructions"],
                model_profile=run["model_profile"],
                attempt_count=state["attempt_count"],
            )
            return ClaimResult("execute", context)

    async def _terminal_receipt(self, connection, job, status, worker_id):
        await connection.execute(
            """INSERT INTO worker_job_receipts
               (job_id,workspace_id,run_id,status,delivery_count,worker_id)
               VALUES (%s,%s,%s,%s,1,%s)
               ON CONFLICT (job_id) DO UPDATE SET
                 status=EXCLUDED.status,
                 delivery_count=worker_job_receipts.delivery_count+1,
                 worker_id=EXCLUDED.worker_id,
                 lease_expires_at=NULL,
                 updated_at=now()""",
            (job.job_id, job.workspace_id, job.run_id, status, worker_id),
        )

    async def _cancel_locked(self, connection, run, job, worker_id):
        if run["status"] != "cancelled":
            await connection.execute(
                """UPDATE agent_runs
                   SET status='cancelled',finished_at=now(),lease_owner=NULL,
                       lease_expires_at=NULL,updated_at=now()
                   WHERE id=%s""",
                (run["id"],),
            )
            await append_run_event(
                connection,
                run["workspace_id"],
                run["id"],
                "run.cancelled",
                {"reason": "requested"},
            )
        await self._terminal_receipt(connection, job, "cancelled", worker_id)

    async def renew_lease(
        self, job_id: UUID, run_id: UUID, worker_id: str, lease_seconds: int
    ) -> bool:
        async with self.connection() as connection:
            receipt = await connection.execute(
                """UPDATE worker_job_receipts
                   SET lease_expires_at=now()+(%s * interval '1 second'),updated_at=now()
                   WHERE job_id=%s AND run_id=%s AND status='processing' AND worker_id=%s
                     AND lease_expires_at > now()""",
                (lease_seconds, job_id, run_id, worker_id),
            )
            run = await connection.execute(
                """UPDATE agent_runs
                   SET lease_expires_at=now()+(%s * interval '1 second'),updated_at=now()
                   WHERE id=%s AND status='running' AND lease_owner=%s
                     AND lease_expires_at > now()""",
                (lease_seconds, run_id, worker_id),
            )
            return receipt.rowcount == 1 and run.rowcount == 1

    async def is_cancel_requested(self, run_id: UUID) -> bool:
        async with self.connection() as connection:
            result = await connection.execute(
                "SELECT status,cancel_requested_at FROM agent_runs WHERE id=%s",
                (run_id,),
            )
            row = await result.fetchone()
            return not row or row["status"] == "cancelled" or row["cancel_requested_at"] is not None

    async def ack_waiting_for_approval(self, job: WorkerJob, worker_id: str) -> bool:
        async with self.connection() as connection:
            run_result = await connection.execute(
                """SELECT status FROM agent_runs
                   WHERE id=%s AND workspace_id=%s FOR UPDATE""",
                (job.run_id, job.workspace_id),
            )
            run = await run_result.fetchone()
            receipt_result = await connection.execute(
                """SELECT status,worker_id,last_error_code FROM worker_job_receipts
                   WHERE job_id=%s FOR UPDATE""",
                (job.job_id,),
            )
            receipt = await receipt_result.fetchone()
            return bool(
                run
                and receipt
                and run["status"] == "waiting_for_approval"
                and receipt["status"] == "superseded"
                and receipt["worker_id"] == worker_id
                and receipt["last_error_code"] == "waiting_for_approval"
            )

    async def complete_success(self, job: WorkerJob, worker_id: str) -> bool:
        async with self.connection() as connection:
            run, receipt = await self._owned_processing(connection, job, worker_id)
            if not run or not receipt:
                return False
            if run["cancel_requested_at"] is not None:
                await self._cancel_locked(connection, run, job, worker_id)
                return True
            await connection.execute(
                """UPDATE agent_runs
                   SET status='succeeded',finished_at=now(),lease_owner=NULL,
                       lease_expires_at=NULL,updated_at=now(),failure_code=NULL
                   WHERE id=%s""",
                (run["id"],),
            )
            await connection.execute(
                """UPDATE worker_job_receipts
                   SET status='succeeded',lease_expires_at=NULL,updated_at=now()
                   WHERE job_id=%s""",
                (job.job_id,),
            )
            await append_run_event(
                connection,
                run["workspace_id"],
                run["id"],
                "run.succeeded",
                {"attempt": run["attempt_count"]},
            )
            return True

    async def complete_failure(
        self, job: WorkerJob, worker_id: str, error_code: str, retryable: bool
    ) -> bool:
        if not error_code or len(error_code) > 100:
            raise ValueError("invalid error_code")
        async with self.connection() as connection:
            run, receipt = await self._owned_processing(connection, job, worker_id)
            if not run or not receipt:
                return False
            if run["cancel_requested_at"] is not None:
                await self._cancel_locked(connection, run, job, worker_id)
                return True
            if retryable and run["attempt_count"] < MAX_RUN_ATTEMPTS:
                delay = retry_delay_seconds(run["id"], run["attempt_count"])
                new_job_id = uuid4()
                await connection.execute(
                    """UPDATE agent_runs
                       SET status='queued',lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                       WHERE id=%s""",
                    (run["id"],),
                )
                await connection.execute(
                    """UPDATE worker_job_receipts
                       SET status='failed',lease_expires_at=NULL,last_error_code=%s,updated_at=now()
                       WHERE job_id=%s""",
                    (error_code, job.job_id),
                )
                await append_run_event(
                    connection,
                    run["workspace_id"],
                    run["id"],
                    "run.retry_scheduled",
                    {
                        "attempt": run["attempt_count"],
                        "error_code": error_code,
                        "delay_seconds": delay,
                    },
                )
                await connection.execute(
                    """INSERT INTO job_outbox
                       (id,workspace_id,run_id,topic,payload,available_at)
                       VALUES (%s,%s,%s,'agent.run.queued.v1',%s,
                               now()+(%s * interval '1 second'))""",
                    (
                        new_job_id,
                        run["workspace_id"],
                        run["id"],
                        Jsonb(
                            {
                                "run_id": str(run["id"]),
                                "workspace_id": str(run["workspace_id"]),
                                "agent_id": str(run["agent_id"]),
                                "trace_id": str(run["trace_id"]),
                            }
                        ),
                        delay,
                    ),
                )
                return True

            await connection.execute(
                """UPDATE agent_runs
                   SET status='failed',finished_at=now(),lease_owner=NULL,
                       lease_expires_at=NULL,updated_at=now(),failure_code=%s
                   WHERE id=%s""",
                (error_code, run["id"]),
            )
            await connection.execute(
                """UPDATE worker_job_receipts
                   SET status='failed',lease_expires_at=NULL,last_error_code=%s,updated_at=now()
                   WHERE job_id=%s""",
                (error_code, job.job_id),
            )
            await append_run_event(
                connection,
                run["workspace_id"],
                run["id"],
                "run.failed",
                {"attempt": run["attempt_count"], "error_code": error_code},
            )
            return True

    async def _owned_processing(self, connection, job, worker_id):
        run_result = await connection.execute(
            "SELECT * FROM agent_runs WHERE id=%s AND workspace_id=%s FOR UPDATE",
            (job.run_id, job.workspace_id),
        )
        run = await run_result.fetchone()
        receipt_result = await connection.execute(
            "SELECT * FROM worker_job_receipts WHERE job_id=%s FOR UPDATE",
            (job.job_id,),
        )
        receipt = await receipt_result.fetchone()
        if (
            not run
            or not receipt
            or run["status"] != "running"
            or run["lease_owner"] != worker_id
            or receipt["status"] != "processing"
            or receipt["worker_id"] != worker_id
        ):
            return None, None
        now_result = await connection.execute("SELECT now() AS now")
        now = (await now_result.fetchone())["now"]
        if (
            run["lease_expires_at"] is None
            or receipt["lease_expires_at"] is None
            or run["lease_expires_at"] <= now
            or receipt["lease_expires_at"] <= now
        ):
            return None, None
        return run, receipt

    async def recover_stale(self, limit: int = 50, orphan_seconds: int = 60) -> int:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not 0 <= orphan_seconds <= 3600:
            raise ValueError("orphan_seconds must be between 0 and 3600")
        recovered = 0
        async with self.connection() as connection:
            expired_result = await connection.execute(
                """SELECT * FROM agent_runs
                   WHERE status='running' AND lease_expires_at < now()
                   ORDER BY lease_expires_at,id
                   FOR UPDATE SKIP LOCKED
                   LIMIT %s""",
                (limit,),
            )
            for run in await expired_result.fetchall():
                await connection.execute(
                    """UPDATE worker_job_receipts
                       SET status='superseded',lease_expires_at=NULL,
                           last_error_code='lease_expired',updated_at=now()
                       WHERE run_id=%s AND status='processing'""",
                    (run["id"],),
                )
                await connection.execute(
                    """UPDATE agent_runs
                       SET status='queued',lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                       WHERE id=%s""",
                    (run["id"],),
                )
                await append_run_event(
                    connection,
                    run["workspace_id"],
                    run["id"],
                    "run.recovered",
                    {"previous_attempt": run["attempt_count"]},
                )
                await self._enqueue_recovery(connection, run)
                recovered += 1

            remaining = max(0, limit - recovered)
            if remaining:
                orphan_result = await connection.execute(
                    """SELECT r.* FROM agent_runs r
                       WHERE r.status='queued'
                         AND r.updated_at < now()-(%s * interval '1 second')
                         AND NOT EXISTS (
                           SELECT 1 FROM job_outbox o
                           WHERE o.run_id=r.id AND o.published_at IS NULL
                             AND o.dead_lettered_at IS NULL
                         )
                       ORDER BY r.updated_at,r.id
                       FOR UPDATE OF r SKIP LOCKED
                       LIMIT %s""",
                    (orphan_seconds, remaining),
                )
                for run in await orphan_result.fetchall():
                    await connection.execute(
                        "UPDATE agent_runs SET updated_at=now() WHERE id=%s",
                        (run["id"],),
                    )
                    await append_run_event(
                        connection,
                        run["workspace_id"],
                        run["id"],
                        "run.redis_requeued",
                        {"reason": "delivery_recovery"},
                    )
                    await self._enqueue_recovery(connection, run)
                    recovered += 1
        return recovered

    async def _enqueue_recovery(self, connection, run):
        await connection.execute(
            """INSERT INTO job_outbox (id,workspace_id,run_id,topic,payload)
               VALUES (%s,%s,%s,'agent.run.queued.v1',%s)""",
            (
                uuid4(),
                run["workspace_id"],
                run["id"],
                Jsonb(
                    {
                        "run_id": str(run["id"]),
                        "workspace_id": str(run["workspace_id"]),
                        "agent_id": str(run["agent_id"]),
                        "trace_id": str(run["trace_id"]),
                    }
                ),
            ),
        )
