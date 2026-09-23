from __future__ import annotations

from dataclasses import dataclass

from nexora_api.embeddings import EmbeddingAdapter
from nexora_api.spend import EmbeddingSpend


@dataclass(frozen=True, slots=True)
class SemanticAnswerCacheConfig:
    model: str
    dimensions: int
    similarity_threshold: float
    timeout_seconds: float
    spend: EmbeddingSpend | None = None

    def __post_init__(self):
        if not self.model:
            raise ValueError("semantic cache model is required")
        if not 1 <= self.dimensions <= 4096:
            raise ValueError("semantic cache dimensions must be between 1 and 4096")
        if not 0.80 <= self.similarity_threshold <= 0.999:
            raise ValueError("semantic cache similarity threshold must be between 0.80 and 0.999")
        if not 0.1 <= self.timeout_seconds <= 120:
            raise ValueError("semantic cache timeout must be between 0.1 and 120 seconds")


@dataclass(frozen=True, slots=True)
class SemanticEmbedding:
    vector: tuple[float, ...]
    input_tokens: int


class SemanticAnswerCache:
    """Provider-backed embeddings used only as a conservative cache lookup accelerator."""

    def __init__(
        self,
        adapter: EmbeddingAdapter,
        config: SemanticAnswerCacheConfig,
    ):
        self.adapter = adapter
        self.config = config

    async def embed(self, text: str, *, request_id: str) -> SemanticEmbedding:
        batch = await self.adapter.embed(
            request_id=request_id,
            texts=(text,),
            model=self.config.model,
            dimensions=self.config.dimensions,
            timeout_seconds=self.config.timeout_seconds,
        )
        if (
            batch.model != self.config.model
            or batch.dimensions != self.config.dimensions
            or len(batch.vectors) != 1
        ):
            raise ValueError("semantic cache embedding adapter returned an incompatible batch")
        return SemanticEmbedding(batch.vectors[0], batch.input_tokens)

    async def aclose(self) -> None:
        closer = getattr(self.adapter, "aclose", None)
        if closer is not None:
            await closer()
