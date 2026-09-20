from __future__ import annotations

import hashlib
import json
import math
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.metrics import observe_spend_denied
from nexora_api.rag import (
    AccessScope,
    AclIdentity,
    RetrievedChunk,
    chunk_text,
    normalize_document,
    sha256_text,
)
from nexora_api.spend import (
    EmbeddingSpend,
    SpendCategory,
    SpendLimitExceeded,
    knowledge_source_key,
)
from nexora_api.spend_repository import evaluate_budget, observe_write, record_spend
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission


class KnowledgeIngestionLeaseLost(RuntimeError):
    pass


class RagRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.workspaces = WorkspaceRepository(settings)

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ) as connection:
            yield connection

    async def authorize_manage(
        self,
        principal: Principal,
        workspace_id: UUID,
    ) -> None:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection,
                principal,
                workspace_id,
                Permission.MANAGE_KNOWLEDGE,
            )

    async def authorize_spend(self, workspace_id: UUID) -> None:
        """Refuse embedding egress once a workspace has spent its budget for the period."""
        async with self.connection() as connection:
            decision = await evaluate_budget(connection, workspace_id)
        if decision.allowed:
            return
        observe_spend_denied(SpendCategory.EMBEDDING)
        raise SpendLimitExceeded()

    async def record_embedding_spend(
        self,
        workspace_id: UUID,
        *,
        source_key: str,
        provider: str,
        model: str,
        input_tokens: int,
        cost_micros: int,
    ) -> bool:
        """Append a priced embedding call that has no other durable row of its own."""
        async with self.connection() as connection:
            write = await record_spend(
                connection,
                workspace_id=workspace_id,
                source_key=source_key,
                category=SpendCategory.EMBEDDING,
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=0,
                cost_micros=cost_micros,
            )
        observe_write(provider, SpendCategory.EMBEDDING, cost_micros, write)
        return write.recorded

    async def authorize_retrieve(
        self,
        principal: Principal,
        workspace_id: UUID,
    ) -> None:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection,
                principal,
                workspace_id,
                Permission.READ,
            )

    @staticmethod
    def _ingestion_view(row):
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "source_key": row["source_key"],
            "version": row["version"],
            "title": row["title"],
            "access_scope": row["access_scope"],
            "status": row["status"],
            "attempt_count": row["attempt_count"],
            "source_id": row["source_id"],
            "chunk_count": row["chunk_count"],
            "embedding_input_tokens": row["embedding_input_tokens"],
            "error_code": row["error_code"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }

    async def enqueue_ingestion(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        body,
        idempotency_key: str,
        request_id: str,
    ):
        normalized_text = normalize_document(body.text)
        acl = sorted(
            {(item.issuer, item.subject) for item in body.acl},
            key=lambda item: (item[0], item[1]),
        )
        normalized = {
            "source_key": body.source_key,
            "version": body.version,
            "title": body.title.strip(),
            "text": normalized_text,
            "access_scope": body.access_scope.value,
            "acl": [{"issuer": issuer, "subject": subject} for issuer, subject in acl],
            "metadata": body.metadata,
            "max_chars": body.max_chars,
            "overlap_chars": body.overlap_chars,
        }
        request_hash = hashlib.sha256(
            json.dumps(
                normalized,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        job_id = uuid4()
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection,
                principal,
                workspace_id,
                Permission.MANAGE_KNOWLEDGE,
            )
            inserted = await connection.execute(
                """INSERT INTO knowledge_ingestion_jobs
                   (id,workspace_id,source_key,version,title,text_content,access_scope,
                    acl,metadata,max_chars,overlap_chars,request_hash,idempotency_key,
                    requested_by_issuer,requested_by_subject)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (
                       workspace_id,requested_by_issuer,requested_by_subject,idempotency_key
                   ) DO NOTHING
                   RETURNING *""",
                (
                    job_id,
                    workspace_id,
                    normalized["source_key"],
                    normalized["version"],
                    normalized["title"],
                    normalized["text"],
                    normalized["access_scope"],
                    Jsonb(normalized["acl"]),
                    Jsonb(normalized["metadata"]),
                    normalized["max_chars"],
                    normalized["overlap_chars"],
                    request_hash,
                    idempotency_key,
                    principal.issuer,
                    principal.subject,
                ),
            )
            row = await inserted.fetchone()
            if row is not None:
                await self.workspaces.audit(
                    connection,
                    principal,
                    workspace_id,
                    "knowledge.ingestion.queued",
                    request_id,
                    body.source_key,
                )
                return self._ingestion_view(row), True

            existing_result = await connection.execute(
                """SELECT * FROM knowledge_ingestion_jobs
                   WHERE workspace_id=%s AND requested_by_issuer=%s
                     AND requested_by_subject=%s AND idempotency_key=%s
                   FOR UPDATE""",
                (workspace_id, principal.issuer, principal.subject, idempotency_key),
            )
            existing = await existing_result.fetchone()
            if existing is None:
                raise RuntimeError("idempotency row disappeared")
            if existing["request_hash"] != request_hash:
                raise HTTPException(409)
            return self._ingestion_view(existing), False

    async def get_ingestion(
        self,
        principal: Principal,
        workspace_id: UUID,
        job_id: UUID,
    ):
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection,
                principal,
                workspace_id,
                Permission.MANAGE_KNOWLEDGE,
            )
            result = await connection.execute(
                """SELECT * FROM knowledge_ingestion_jobs
                   WHERE workspace_id=%s AND id=%s""",
                (workspace_id, job_id),
            )
            row = await result.fetchone()
            if row is None:
                raise HTTPException(404)
            return self._ingestion_view(row)

    async def list_sources(
        self,
        principal: Principal,
        workspace_id: UUID,
        limit: int,
        cursor: UUID | None,
    ):
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection,
                principal,
                workspace_id,
                Permission.READ,
            )
            result = await connection.execute(
                """SELECT s.id,s.source_key,s.version,s.title,s.access_scope,
                          s.content_hash,s.created_at,s.updated_at,
                          count(c.id)::integer AS chunk_count
                   FROM rag_sources s
                   LEFT JOIN rag_chunks c
                     ON c.source_id=s.id AND c.workspace_id=s.workspace_id
                   WHERE s.workspace_id=%s
                     AND s.is_current
                     AND (%s::uuid IS NULL OR s.id > %s::uuid)
                     AND (
                         s.access_scope='workspace'
                         OR EXISTS (
                             SELECT 1 FROM rag_source_acl a
                             WHERE a.workspace_id=s.workspace_id
                               AND a.source_id=s.id
                               AND a.issuer=%s
                               AND a.subject=%s
                         )
                     )
                   GROUP BY s.id
                   ORDER BY s.id
                   LIMIT %s""",
                (
                    workspace_id,
                    cursor,
                    cursor,
                    principal.issuer,
                    principal.subject,
                    limit,
                ),
            )
            return [dict(row) for row in await result.fetchall()]

    async def index_source(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        source_key: str,
        version: str,
        title: str,
        text: str,
        embedding_model: str,
        embeddings: tuple[tuple[float, ...], ...],
        request_id: str,
        access_scope: AccessScope = AccessScope.WORKSPACE,
        acl: tuple[AclIdentity, ...] = (),
        metadata: dict[str, Any] | None = None,
        max_chars: int = 1200,
        overlap_chars: int = 120,
        ingestion_job_id: UUID | None = None,
        ingestion_worker_id: str | None = None,
        embedding_input_tokens: int | None = None,
        embedding_spend: EmbeddingSpend | None = None,
    ) -> UUID:
        self._validate_source_fields(source_key, version, title, embedding_model)
        normalized = normalize_document(text)
        chunks = chunk_text(
            normalized,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
        vectors = tuple(self._vector_literal(vector) for vector in embeddings)
        if len(vectors) != len(chunks):
            raise ValueError("one embedding is required for every deterministic chunk")
        dimensions = len(embeddings[0]) if embeddings else 0
        if not dimensions:
            raise ValueError("at least one embedding is required")
        if any(len(vector) != dimensions for vector in embeddings):
            raise ValueError("all embeddings must use the same dimensions")

        identities = tuple(sorted(set(acl), key=lambda item: (item.issuer, item.subject)))
        if access_scope == AccessScope.RESTRICTED and not identities:
            raise ValueError("restricted sources require at least one ACL identity")
        if access_scope == AccessScope.WORKSPACE and identities:
            raise ValueError("workspace sources must not include explicit ACL identities")

        if (ingestion_job_id is None) != (ingestion_worker_id is None):
            raise ValueError("ingestion fencing arguments must be provided together")
        if ingestion_job_id is not None and (
            embedding_input_tokens is None or embedding_input_tokens < 0
        ):
            raise ValueError("embedding_input_tokens is required for ingestion completion")

        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection,
                principal,
                workspace_id,
                Permission.MANAGE_KNOWLEDGE,
            )
            if ingestion_job_id is not None:
                fence_result = await connection.execute(
                    """SELECT id FROM knowledge_ingestion_jobs
                       WHERE id=%s AND workspace_id=%s AND source_key=%s AND version=%s
                         AND requested_by_issuer=%s AND requested_by_subject=%s
                         AND status='running' AND lease_owner=%s
                         AND lease_expires_at > clock_timestamp()
                       FOR UPDATE""",
                    (
                        ingestion_job_id,
                        workspace_id,
                        source_key,
                        version,
                        principal.issuer,
                        principal.subject,
                        ingestion_worker_id,
                    ),
                )
                if await fence_result.fetchone() is None:
                    raise KnowledgeIngestionLeaseLost("knowledge_ingestion_lease_lost")

            existing_result = await connection.execute(
                """SELECT id FROM rag_sources
                   WHERE workspace_id=%s AND source_key=%s AND version=%s
                   FOR UPDATE""",
                (workspace_id, source_key, version),
            )
            existing = await existing_result.fetchone()

            await connection.execute(
                """UPDATE rag_sources SET is_current=false,updated_at=now()
                   WHERE workspace_id=%s AND source_key=%s AND is_current""",
                (workspace_id, source_key),
            )

            source_id = existing["id"] if existing else uuid4()
            if existing:
                await connection.execute(
                    """UPDATE rag_sources
                       SET title=%s,content_hash=%s,access_scope=%s,
                           is_current=true,updated_at=now()
                       WHERE id=%s AND workspace_id=%s""",
                    (
                        title,
                        sha256_text(normalized),
                        access_scope,
                        source_id,
                        workspace_id,
                    ),
                )
            else:
                await connection.execute(
                    """INSERT INTO rag_sources
                       (id,workspace_id,source_key,version,title,content_hash,access_scope,
                        is_current,created_by_issuer,created_by_subject)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,true,%s,%s)""",
                    (
                        source_id,
                        workspace_id,
                        source_key,
                        version,
                        title,
                        sha256_text(normalized),
                        access_scope,
                        principal.issuer,
                        principal.subject,
                    ),
                )

            await connection.execute(
                "DELETE FROM rag_source_acl WHERE source_id=%s AND workspace_id=%s",
                (source_id, workspace_id),
            )
            await connection.execute(
                "DELETE FROM rag_chunks WHERE source_id=%s AND workspace_id=%s",
                (source_id, workspace_id),
            )

            for identity in identities:
                await connection.execute(
                    """INSERT INTO rag_source_acl
                       (workspace_id,source_id,issuer,subject)
                       VALUES (%s,%s,%s,%s)""",
                    (workspace_id, source_id, identity.issuer, identity.subject),
                )

            chunk_metadata = metadata or {}
            for chunk, vector_literal in zip(chunks, vectors, strict=True):
                await connection.execute(
                    """INSERT INTO rag_chunks
                       (id,workspace_id,source_id,source_version,chunk_index,
                        start_offset,end_offset,content,content_hash,metadata,
                        access_scope,embedding_model,embedding_dimensions,embedding)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)""",
                    (
                        uuid4(),
                        workspace_id,
                        source_id,
                        version,
                        chunk.chunk_index,
                        chunk.start_offset,
                        chunk.end_offset,
                        chunk.content,
                        chunk.content_hash,
                        Jsonb(chunk_metadata),
                        access_scope,
                        embedding_model,
                        dimensions,
                        vector_literal,
                    ),
                )

            if ingestion_job_id is not None:
                completed = await connection.execute(
                    """UPDATE knowledge_ingestion_jobs
                       SET status='succeeded',source_id=%s,chunk_count=%s,
                           embedding_input_tokens=%s,lease_owner=NULL,lease_expires_at=NULL,
                           error_code=NULL,finished_at=now(),updated_at=now()
                       WHERE id=%s AND status='running' AND lease_owner=%s
                         AND lease_expires_at > clock_timestamp()""",
                    (
                        source_id,
                        len(chunks),
                        embedding_input_tokens,
                        ingestion_job_id,
                        ingestion_worker_id,
                    ),
                )
                if completed.rowcount != 1:
                    raise KnowledgeIngestionLeaseLost("knowledge_ingestion_lease_lost")

            # The ledger row commits with the indexed version it pays for, so a
            # retried job never charges the same stored embeddings twice.
            recorded_cost = None
            write = None
            if embedding_spend is not None and embedding_input_tokens is not None:
                recorded_cost = embedding_spend.cost_micros(embedding_input_tokens)
                write = await record_spend(
                    connection,
                    workspace_id=workspace_id,
                    source_key=knowledge_source_key(source_id),
                    category=SpendCategory.EMBEDDING,
                    provider=embedding_spend.provider,
                    model=embedding_spend.model,
                    input_tokens=embedding_input_tokens,
                    output_tokens=0,
                    cost_micros=recorded_cost,
                )

            await self.workspaces.audit(
                connection,
                principal,
                workspace_id,
                "knowledge.source.indexed",
                request_id,
                source_key,
            )
        if write is not None:
            observe_write(embedding_spend.provider, SpendCategory.EMBEDDING, recorded_cost, write)
        return source_id

    async def delete_source(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        source_key: str,
        request_id: str,
    ) -> int:
        if not 1 <= len(source_key) <= 255:
            raise ValueError("source_key must be between 1 and 255 characters")

        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection,
                principal,
                workspace_id,
                Permission.MANAGE_KNOWLEDGE,
            )
            pending = await connection.execute(
                """SELECT id,status,lease_expires_at,clock_timestamp() AS checked_at
                   FROM knowledge_ingestion_jobs
                   WHERE workspace_id=%s AND source_key=%s
                     AND status IN ('queued','running')
                   FOR UPDATE""",
                (workspace_id, source_key),
            )
            pending_rows = await pending.fetchall()
            if any(
                row["status"] == "running"
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > row["checked_at"]
                for row in pending_rows
            ):
                raise HTTPException(409)

            cancelled = await connection.execute(
                """UPDATE knowledge_ingestion_jobs
                   SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,
                       finished_at=now(),updated_at=now()
                   WHERE workspace_id=%s AND source_key=%s
                     AND status IN ('queued','running')""",
                (workspace_id, source_key),
            )
            deleted = await connection.execute(
                "DELETE FROM rag_sources WHERE workspace_id=%s AND source_key=%s",
                (workspace_id, source_key),
            )
            count = deleted.rowcount or 0
            if count or (cancelled.rowcount or 0):
                await self.workspaces.audit(
                    connection,
                    principal,
                    workspace_id,
                    "knowledge.source.deleted",
                    request_id,
                    source_key,
                )
            return count

    async def retrieve(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        embedding_model: str,
        query_embedding: tuple[float, ...],
        limit: int = 8,
    ) -> tuple[RetrievedChunk, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if not 1 <= len(embedding_model) <= 255:
            raise ValueError("embedding_model must be between 1 and 255 characters")

        vector_literal = self._vector_literal(query_embedding)
        dimensions = len(query_embedding)

        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection,
                principal,
                workspace_id,
                Permission.READ,
            )
            result = await connection.execute(
                """SELECT c.id,c.source_id,s.source_key,c.source_version,s.title,
                          c.chunk_index,c.start_offset,c.end_offset,c.content,c.metadata,
                          1 - (c.embedding <=> %s::vector) AS score
                   FROM rag_chunks c
                   JOIN rag_sources s
                     ON s.id=c.source_id AND s.workspace_id=c.workspace_id
                   WHERE c.workspace_id=%s
                     AND s.is_current
                     AND c.embedding_model=%s
                     AND c.embedding_dimensions=%s
                     AND (
                         c.access_scope='workspace'
                         OR EXISTS (
                             SELECT 1 FROM rag_source_acl a
                             WHERE a.workspace_id=c.workspace_id
                               AND a.source_id=c.source_id
                               AND a.issuer=%s
                               AND a.subject=%s
                         )
                     )
                   ORDER BY c.embedding <=> %s::vector,c.source_id,c.chunk_index
                   LIMIT %s""",
                (
                    vector_literal,
                    workspace_id,
                    embedding_model,
                    dimensions,
                    principal.issuer,
                    principal.subject,
                    vector_literal,
                    limit,
                ),
            )
            rows = await result.fetchall()
            return tuple(
                RetrievedChunk(
                    id=row["id"],
                    source_id=row["source_id"],
                    source_key=row["source_key"],
                    source_version=row["source_version"],
                    title=row["title"],
                    chunk_index=row["chunk_index"],
                    start_offset=row["start_offset"],
                    end_offset=row["end_offset"],
                    content=row["content"],
                    metadata=row["metadata"],
                    score=float(row["score"]),
                )
                for row in rows
            )

    @staticmethod
    def _validate_source_fields(
        source_key: str,
        version: str,
        title: str,
        embedding_model: str,
    ) -> None:
        if not 1 <= len(source_key) <= 255:
            raise ValueError("source_key must be between 1 and 255 characters")
        if not 1 <= len(version) <= 128:
            raise ValueError("version must be between 1 and 128 characters")
        if not 1 <= len(title.strip()) <= 500:
            raise ValueError("title must be between 1 and 500 characters")
        if not 1 <= len(embedding_model) <= 255:
            raise ValueError("embedding_model must be between 1 and 255 characters")

    @staticmethod
    def _vector_literal(vector: tuple[float, ...]) -> str:
        if not 1 <= len(vector) <= 4096:
            raise ValueError("embedding dimensions must be between 1 and 4096")
        values = tuple(float(value) for value in vector)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("embedding values must be finite")
        if not any(value != 0 for value in values):
            raise ValueError("cosine embeddings must not be the zero vector")
        return "[" + ",".join(format(value, ".9g") for value in values) + "]"
