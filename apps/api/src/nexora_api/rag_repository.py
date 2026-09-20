from __future__ import annotations

import math
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.rag import (
    AccessScope,
    AclIdentity,
    RetrievedChunk,
    chunk_text,
    normalize_document,
    sha256_text,
)
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission


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

        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection,
                principal,
                workspace_id,
                Permission.MANAGE_KNOWLEDGE,
            )
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

            await self.workspaces.audit(
                connection,
                principal,
                workspace_id,
                "knowledge.source.indexed",
                request_id,
                source_key,
            )
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
            deleted = await connection.execute(
                "DELETE FROM rag_sources WHERE workspace_id=%s AND source_key=%s",
                (workspace_id, source_key),
            )
            count = deleted.rowcount or 0
            if count:
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
