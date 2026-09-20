import asyncio
from uuid import uuid4

import pytest

from nexora_api.auth import Principal
from nexora_api.embeddings import EmbeddingBatch
from nexora_api.rag import RetrievedChunk
from nexora_api.rag_pipeline import RagEmbeddingPipeline


class StubAdapter:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.calls = 0

    async def embed(self, *, request_id, texts, model, dimensions, timeout_seconds):
        self.events.append("embed")
        self.calls += 1
        vectors = tuple((1.0,) + (0.0,) * (dimensions - 1) for _ in texts)
        return EmbeddingBatch(vectors, model, dimensions, len(texts))

    async def cancel(self, request_id):
        return None

    async def aclose(self):
        self.closed = True


class StubRepository:
    def __init__(self, events, *, reject=False, chunks=()):
        self.events = events
        self.reject = reject
        self.chunks = chunks
        self.indexed_embeddings = None

    async def authorize_manage(self, principal, workspace_id):
        self.events.append("authorize_manage")
        if self.reject:
            raise PermissionError("denied")

    async def authorize_retrieve(self, principal, workspace_id):
        self.events.append("authorize_retrieve")
        if self.reject:
            raise PermissionError("denied")

    async def retrieve(self, principal, workspace_id, **kwargs):
        self.events.append("retrieve")
        return self.chunks

    async def index_source(self, principal, workspace_id, **kwargs):
        self.events.append("index")
        self.indexed_embeddings = kwargs["embeddings"]
        return uuid4()


def principal():
    return Principal(issuer="issuer", subject="subject")


def chunk():
    return RetrievedChunk(
        id=uuid4(),
        source_id=uuid4(),
        source_key="handbook",
        source_version="v1",
        title="Handbook",
        chunk_index=2,
        start_offset=10,
        end_offset=24,
        content="approved evidence",
        metadata={},
        score=0.9,
    )


def test_context_authorizes_before_provider_and_preserves_citation_boundary():
    events = []
    repository = StubRepository(events, chunks=(chunk(),))
    adapter = StubAdapter(events)
    pipeline = RagEmbeddingPipeline(
        repository,
        adapter,
        embedding_model="embed-test",
        dimensions=3,
        retrieval_limit=5,
    )

    result = asyncio.run(pipeline.context(principal(), uuid4(), "what is approved?"))

    assert events == ["authorize_retrieve", "embed", "retrieve"]
    assert "UNTRUSTED RETRIEVED EVIDENCE" in result
    assert "source=handbook version=v1 chunk=2" in result
    assert "approved evidence" in result


def test_unauthorized_retrieval_cannot_trigger_paid_embedding():
    events = []
    adapter = StubAdapter(events)
    pipeline = RagEmbeddingPipeline(
        StubRepository(events, reject=True),
        adapter,
        embedding_model="embed-test",
        dimensions=3,
    )

    with pytest.raises(PermissionError):
        asyncio.run(pipeline.context(principal(), uuid4(), "secret query"))

    assert events == ["authorize_retrieve"]
    assert adapter.calls == 0


def test_indexing_authorizes_before_embedding_and_persists_one_vector_per_chunk():
    events = []
    repository = StubRepository(events)
    adapter = StubAdapter(events)
    pipeline = RagEmbeddingPipeline(
        repository,
        adapter,
        embedding_model="embed-test",
        dimensions=3,
    )

    result = asyncio.run(
        pipeline.index_source(
            principal(),
            uuid4(),
            source_key="guide",
            version="1",
            title="Guide",
            text="A" * 500,
            request_id="index-request",
            max_chars=300,
            overlap_chars=20,
        )
    )

    assert events[0:2] == ["authorize_manage", "embed"]
    assert events[-1] == "index"
    assert result.chunk_count == len(repository.indexed_embeddings)
    assert result.embedding_input_tokens == result.chunk_count


def test_pipeline_close_releases_embedding_transport():
    events = []
    adapter = StubAdapter(events)
    pipeline = RagEmbeddingPipeline(
        StubRepository(events),
        adapter,
        embedding_model="embed-test",
        dimensions=3,
    )

    asyncio.run(pipeline.aclose())

    assert adapter.closed
