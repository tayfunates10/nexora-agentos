from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from nexora_api.auth import Principal
from nexora_api.embeddings import EmbeddingAdapter
from nexora_api.rag import (
    AccessScope,
    AclIdentity,
    RetrievedChunk,
    build_untrusted_context,
    chunk_text,
)
from nexora_api.rag_repository import RagRepository
from nexora_api.spend import EmbeddingSpend, SpendPricingError, adhoc_retrieval_source_key


@dataclass(frozen=True, slots=True)
class RagIndexResult:
    source_id: UUID
    chunk_count: int
    embedding_input_tokens: int


@dataclass(frozen=True, slots=True)
class RagSearchResult:
    chunks: tuple[RetrievedChunk, ...]
    embedding_input_tokens: int


class RagEmbeddingPipeline:
    def __init__(
        self,
        repository: RagRepository,
        adapter: EmbeddingAdapter,
        *,
        embedding_model: str,
        dimensions: int,
        batch_size: int = 128,
        timeout_seconds: float = 15.0,
        retrieval_limit: int = 8,
        spend: EmbeddingSpend | None = None,
    ):
        if not 1 <= dimensions <= 4096:
            raise ValueError("dimensions must be between 1 and 4096")
        if not 1 <= batch_size <= 256:
            raise ValueError("batch_size must be between 1 and 256")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= retrieval_limit <= 50:
            raise ValueError("retrieval_limit must be between 1 and 50")
        self.repository = repository
        self.adapter = adapter
        self.embedding_model = embedding_model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds
        self.retrieval_limit = retrieval_limit
        # Without operator pricing, embedding accounting and budgets stay off rather
        # than recording a fabricated zero cost.
        self.spend = spend

    async def index_source(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        source_key: str,
        version: str,
        title: str,
        text: str,
        request_id: str,
        access_scope: AccessScope = AccessScope.WORKSPACE,
        acl: tuple[AclIdentity, ...] = (),
        metadata: dict[str, Any] | None = None,
        max_chars: int = 1200,
        overlap_chars: int = 120,
        ingestion_job_id: UUID | None = None,
        ingestion_worker_id: str | None = None,
    ) -> RagIndexResult:
        # Do not trigger paid provider work for an unauthorized caller.
        # The repository rechecks permission inside the write transaction.
        await self.repository.authorize_manage(principal, workspace_id)
        if self.spend is not None:
            await self.repository.authorize_spend(workspace_id)

        chunks = chunk_text(text, max_chars=max_chars, overlap_chars=overlap_chars)
        vectors, input_tokens = await self._embed_texts(
            tuple(chunk.content for chunk in chunks),
            request_id=request_id + ":index",
        )
        source_id = await self.repository.index_source(
            principal,
            workspace_id,
            source_key=source_key,
            version=version,
            title=title,
            text=text,
            embedding_model=self.embedding_model,
            embeddings=vectors,
            request_id=request_id,
            access_scope=access_scope,
            acl=acl,
            metadata=metadata,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            ingestion_job_id=ingestion_job_id,
            ingestion_worker_id=ingestion_worker_id,
            embedding_input_tokens=input_tokens,
            embedding_spend=self.spend,
        )
        return RagIndexResult(
            source_id=source_id,
            chunk_count=len(chunks),
            embedding_input_tokens=input_tokens,
        )

    async def retrieve(
        self,
        principal: Principal,
        workspace_id: UUID,
        *,
        query: str,
        request_id: str,
        limit: int | None = None,
        spend_key: str | None = None,
    ) -> RagSearchResult:
        # Membership is checked before provider egress and again in retrieval SQL.
        await self.repository.authorize_retrieve(principal, workspace_id)
        if self.spend is not None:
            # A query embedding with nowhere to charge it would be unmetered egress.
            if spend_key is None:
                raise SpendPricingError("embedding_spend_key_missing")
            await self.repository.authorize_spend(workspace_id)
        vectors, input_tokens = await self._embed_texts(
            (query,),
            request_id=request_id + ":query",
        )
        if self.spend is not None and spend_key is not None:
            # A query embedding has no durable artifact of its own: record the paid
            # call now, so a later run failure cannot erase what was already spent.
            await self.repository.record_embedding_spend(
                workspace_id,
                source_key=spend_key,
                provider=self.spend.provider,
                model=self.spend.model,
                input_tokens=input_tokens,
                cost_micros=self.spend.cost_micros(input_tokens),
            )
        chunks = await self.repository.retrieve(
            principal,
            workspace_id,
            embedding_model=self.embedding_model,
            query_embedding=vectors[0],
            limit=self.retrieval_limit if limit is None else limit,
        )
        return RagSearchResult(chunks=chunks, embedding_input_tokens=input_tokens)

    async def context(
        self,
        principal: Principal,
        workspace_id: UUID,
        query: str,
    ) -> str:
        request_id = uuid4()
        result = await self.retrieve(
            principal,
            workspace_id,
            query=query,
            request_id=f"worker-retrieval:{request_id}",
            spend_key=adhoc_retrieval_source_key(request_id),
        )
        if not result.chunks:
            return ""
        return build_untrusted_context(result.chunks)

    async def aclose(self) -> None:
        closer = getattr(self.adapter, "aclose", None)
        if closer is not None:
            await closer()

    async def _embed_texts(
        self,
        texts: tuple[str, ...],
        *,
        request_id: str,
    ) -> tuple[tuple[tuple[float, ...], ...], int]:
        vectors: list[tuple[float, ...]] = []
        input_tokens = 0
        for batch_no, start in enumerate(range(0, len(texts), self.batch_size)):
            batch = texts[start : start + self.batch_size]
            result = await self.adapter.embed(
                request_id=f"{request_id}:{batch_no}",
                texts=batch,
                model=self.embedding_model,
                dimensions=self.dimensions,
                timeout_seconds=self.timeout_seconds,
            )
            if result.model != self.embedding_model or result.dimensions != self.dimensions:
                raise ValueError("embedding adapter returned an incompatible batch")
            vectors.extend(result.vectors)
            input_tokens += result.input_tokens

        if len(vectors) != len(texts):
            raise ValueError("embedding adapter returned the wrong number of vectors")
        return tuple(vectors), input_tokens
