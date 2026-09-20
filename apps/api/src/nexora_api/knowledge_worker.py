import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.model_routing import ProviderError
from nexora_api.rag import AccessScope, AclIdentity
from nexora_api.rag_pipeline import RagEmbeddingPipeline

MAX_INGESTION_ATTEMPTS = 3


def ingestion_retry_delay(job_id: UUID, attempt_count: int) -> float:
    base = min(60.0, float(2 ** min(attempt_count, 6)))
    digest = hashlib.sha256(f"{job_id}:{attempt_count}".encode()).digest()
    jitter = int.from_bytes(digest[:2], "big") / 65535
    return round(base * (1 + 0.25 * jitter), 3)


class KnowledgeIngestionWorker:
    def __init__(
        self,
        settings: Settings,
        pipeline: RagEmbeddingPipeline,
        *,
        worker_id: str,
        lease_seconds: int = 30,
    ):
        if not 3 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 3 and 300")
        self.settings = settings
        self.pipeline = pipeline
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ) as connection:
            yield connection

    async def process_once(self) -> bool:
        job = await self._claim()
        if job is None:
            return False

        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(job["id"], stop))
        try:
            principal = Principal(job["requested_by_issuer"], job["requested_by_subject"])
            acl = tuple(
                AclIdentity(item["issuer"], item["subject"])
                for item in job["acl"]
            )
            result = await self.pipeline.index_source(
                principal,
                job["workspace_id"],
                source_key=job["source_key"],
                version=job["version"],
                title=job["title"],
                text=job["text_content"],
                request_id=f"knowledge-ingestion:{job['id']}",
                access_scope=AccessScope(job["access_scope"]),
                acl=acl,
                metadata=job["metadata"],
                max_chars=job["max_chars"],
                overlap_chars=job["overlap_chars"],
            )
        except HTTPException:
            await self._fail(job, "knowledge_permission_revoked", retryable=False)
        except ProviderError as exc:
            await self._fail(job, exc.code, retryable=exc.retryable)
        except ValueError:
            await self._fail(job, "knowledge_invalid_source", retryable=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail(job, "knowledge_ingestion_error", retryable=True)
        else:
            await self._succeed(job, result.source_id, result.chunk_count, result.embedding_input_tokens)
        finally:
            stop.set()
            await heartbeat
        return True

    async def _claim(self):
        async with self.connection() as connection:
            result = await connection.execute(
                """SELECT * FROM knowledge_ingestion_jobs
                   WHERE (
                       status='queued' AND available_at <= now()
                   ) OR (
                       status='running' AND lease_expires_at < now()
                   )
                   ORDER BY available_at,created_at,id
                   FOR UPDATE SKIP LOCKED
                   LIMIT 1"""
            )
            job = await result.fetchone()
            if not job:
                return None
            if job["status"] == "running":
                await connection.execute(
                    """UPDATE knowledge_ingestion_jobs
                       SET status='queued',lease_owner=NULL,lease_expires_at=NULL,
                           updated_at=now(),error_code='lease_expired'
                       WHERE id=%s""",
                    (job["id"],),
                )
            updated = await connection.execute(
                """UPDATE knowledge_ingestion_jobs
                   SET status='running',attempt_count=attempt_count+1,
                       lease_owner=%s,lease_expires_at=now()+(%s * interval '1 second'),
                       error_code=NULL,updated_at=now()
                   WHERE id=%s
                   RETURNING *""",
                (self.worker_id, self.lease_seconds, job["id"]),
            )
            return await updated.fetchone()

    async def _heartbeat(self, job_id: UUID, stop: asyncio.Event):
        interval = max(1.0, self.lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                async with self.connection() as connection:
                    renewed = await connection.execute(
                        """UPDATE knowledge_ingestion_jobs
                           SET lease_expires_at=now()+(%s * interval '1 second'),
                               updated_at=now()
                           WHERE id=%s AND status='running' AND lease_owner=%s
                             AND lease_expires_at > now()""",
                        (self.lease_seconds, job_id, self.worker_id),
                    )
                    if renewed.rowcount != 1:
                        return

    async def _owned(self, connection, job_id: UUID):
        result = await connection.execute(
            """SELECT * FROM knowledge_ingestion_jobs
               WHERE id=%s FOR UPDATE""",
            (job_id,),
        )
        row = await result.fetchone()
        if not row:
            return None
        now_result = await connection.execute("SELECT now() AS now")
        now: datetime = (await now_result.fetchone())["now"]
        if (
            row["status"] != "running"
            or row["lease_owner"] != self.worker_id
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= now
        ):
            return None
        return row

    async def _succeed(
        self,
        job,
        source_id: UUID,
        chunk_count: int,
        embedding_input_tokens: int,
    ):
        async with self.connection() as connection:
            owned = await self._owned(connection, job["id"])
            if not owned:
                return False
            await connection.execute(
                """UPDATE knowledge_ingestion_jobs
                   SET status='succeeded',source_id=%s,chunk_count=%s,
                       embedding_input_tokens=%s,lease_owner=NULL,lease_expires_at=NULL,
                       finished_at=now(),updated_at=now()
                   WHERE id=%s""",
                (source_id, chunk_count, embedding_input_tokens, job["id"]),
            )
            return True

    async def _fail(self, job, error_code: str, *, retryable: bool):
        async with self.connection() as connection:
            owned = await self._owned(connection, job["id"])
            if not owned:
                return False
            if retryable and owned["attempt_count"] < MAX_INGESTION_ATTEMPTS:
                delay = ingestion_retry_delay(job["id"], owned["attempt_count"])
                await connection.execute(
                    """UPDATE knowledge_ingestion_jobs
                       SET status='queued',available_at=now()+(%s * interval '1 second'),
                           lease_owner=NULL,lease_expires_at=NULL,error_code=%s,updated_at=now()
                       WHERE id=%s""",
                    (delay, error_code[:100], job["id"]),
                )
            else:
                await connection.execute(
                    """UPDATE knowledge_ingestion_jobs
                       SET status='failed',lease_owner=NULL,lease_expires_at=NULL,
                           error_code=%s,finished_at=now(),updated_at=now()
                       WHERE id=%s""",
                    (error_code[:100], job["id"]),
                )
            return True
