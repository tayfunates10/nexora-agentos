from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from nexora_api.model_routing import ProviderError

_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    model: str
    dimensions: int
    input_tokens: int


class EmbeddingAdapter(Protocol):
    @property
    def name(self) -> str: ...

    async def embed(
        self,
        *,
        request_id: str,
        texts: tuple[str, ...],
        model: str,
        dimensions: int,
        timeout_seconds: float,
    ) -> EmbeddingBatch: ...

    async def cancel(self, request_id: str) -> None: ...


class OpenAIEmbeddingsAdapter:
    """Fixed-egress OpenAI embeddings adapter with bounded normalized output."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model_dimensions: Mapping[str, frozenset[int]],
        client: httpx.AsyncClient | None = None,
    ):
        if not api_key.get_secret_value():
            raise ValueError("api_key must not be empty")
        if not model_dimensions:
            raise ValueError("model_dimensions must not be empty")
        self._api_key = api_key
        self._model_dimensions = {
            model: frozenset(dimensions) for model, dimensions in model_dimensions.items()
        }
        if any(
            not dimensions or any(dimension < 1 or dimension > 4096 for dimension in dimensions)
            for dimensions in self._model_dimensions.values()
        ):
            raise ValueError("configured embedding dimensions must be between 1 and 4096")
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}

    @property
    def name(self) -> str:
        return "openai"

    async def embed(
        self,
        *,
        request_id: str,
        texts: tuple[str, ...],
        model: str,
        dimensions: int,
        timeout_seconds: float,
    ) -> EmbeddingBatch:
        self._validate_request(request_id, texts, model, dimensions)
        task = self._register(request_id)
        try:
            response = await self._client.post(
                _EMBEDDINGS_URL,
                headers=self._headers(),
                json={
                    "model": model,
                    "input": list(texts),
                    "encoding_format": "float",
                    "dimensions": dimensions,
                },
                timeout=timeout_seconds,
            )
            self._raise_for_status(response)
            return self._parse_response(
                response,
                expected_count=len(texts),
                expected_model=model,
                expected_dimensions=dimensions,
            )
        except asyncio.CancelledError as exc:
            raise ProviderError("embedding_cancelled") from exc
        except httpx.TimeoutException as exc:
            raise ProviderError("embedding_timeout", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderError("embedding_unavailable", retryable=True) from exc
        finally:
            self._unregister(request_id, task)

    async def cancel(self, request_id: str) -> None:
        task = self._active_tasks.get(request_id)
        if task is not None and not task.done():
            task.cancel()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _validate_request(
        self,
        request_id: str,
        texts: tuple[str, ...],
        model: str,
        dimensions: int,
    ) -> None:
        if not request_id or not request_id.strip():
            raise ProviderError("embedding_request_id_required")
        configured = self._model_dimensions.get(model)
        if configured is None:
            raise ProviderError("embedding_model_not_configured")
        if dimensions not in configured:
            raise ProviderError("embedding_dimensions_not_configured")
        if not 1 <= len(texts) <= 256:
            raise ProviderError("embedding_batch_size_invalid")
        if any(not text.strip() or len(text) > 12000 for text in texts):
            raise ProviderError("embedding_input_invalid")

    def _register(self, request_id: str) -> asyncio.Task[Any]:
        current = asyncio.current_task()
        if current is None:
            raise ProviderError("embedding_runtime_unavailable", retryable=True)
        existing = self._active_tasks.get(request_id)
        if existing is not None and not existing.done():
            raise ProviderError("duplicate_embedding_request")
        self._active_tasks[request_id] = current
        return current

    def _unregister(self, request_id: str, task: asyncio.Task[Any]) -> None:
        if self._active_tasks.get(request_id) is task:
            self._active_tasks.pop(request_id, None)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self._api_key.get_secret_value(),
            "Content-Type": "application/json",
        }

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status == 429:
            raise ProviderError("embedding_rate_limited", retryable=True)
        if status in {408, 409} or 500 <= status < 600:
            raise ProviderError("embedding_unavailable", retryable=True)
        if status in {401, 403}:
            raise ProviderError("embedding_auth_error")
        if status in {400, 404, 422}:
            raise ProviderError("embedding_bad_request")
        raise ProviderError("embedding_http_error")

    @staticmethod
    def _decode_json(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise ProviderError("embedding_invalid_response") from exc
        if not isinstance(value, dict):
            raise ProviderError("embedding_invalid_response")
        return value

    def _parse_response(
        self,
        response: httpx.Response,
        *,
        expected_count: int,
        expected_model: str,
        expected_dimensions: int,
    ) -> EmbeddingBatch:
        payload = self._decode_json(response)
        data = payload.get("data")
        model = payload.get("model")
        usage = payload.get("usage")
        if not isinstance(data, list) or not isinstance(model, str) or not isinstance(usage, dict):
            raise ProviderError("embedding_invalid_response")
        if model != expected_model:
            raise ProviderError("embedding_model_mismatch")

        indexed: dict[int, tuple[float, ...]] = {}
        for item in data:
            if not isinstance(item, dict):
                raise ProviderError("embedding_invalid_response")
            index = item.get("index")
            embedding = item.get("embedding")
            if not isinstance(index, int) or not isinstance(embedding, list):
                raise ProviderError("embedding_invalid_response")
            if index in indexed or not 0 <= index < expected_count:
                raise ProviderError("embedding_invalid_response")
            vector = self._normalize_vector(embedding, expected_dimensions)
            indexed[index] = vector

        if set(indexed) != set(range(expected_count)):
            raise ProviderError("embedding_invalid_response")

        input_tokens = usage.get("prompt_tokens")
        if not isinstance(input_tokens, int) or input_tokens < 0:
            raise ProviderError("embedding_invalid_response")

        return EmbeddingBatch(
            vectors=tuple(indexed[index] for index in range(expected_count)),
            model=model,
            dimensions=expected_dimensions,
            input_tokens=input_tokens,
        )

    @staticmethod
    def _normalize_vector(values: list[Any], expected_dimensions: int) -> tuple[float, ...]:
        if len(values) != expected_dimensions:
            raise ProviderError("embedding_dimension_mismatch")
        try:
            vector = tuple(float(value) for value in values)
        except (TypeError, ValueError) as exc:
            raise ProviderError("embedding_invalid_response") from exc
        if any(not math.isfinite(value) for value in vector):
            raise ProviderError("embedding_invalid_response")
        if not any(value != 0 for value in vector):
            raise ProviderError("embedding_zero_vector")
        return vector
