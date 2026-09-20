import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from nexora_api.embeddings import OpenAIEmbeddingsAdapter
from nexora_api.model_routing import ProviderError


def adapter(client: httpx.AsyncClient) -> OpenAIEmbeddingsAdapter:
    return OpenAIEmbeddingsAdapter(
        api_key=SecretStr("test-embedding-key"),
        model_dimensions={"embed-test": frozenset({3, 4})},
        client=client,
    )


def test_embedding_adapter_serializes_batch_and_restores_index_order():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "embed-test",
                "data": [
                    {"object": "embedding", "index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"object": "embedding", "index": 0, "embedding": [1.0, 0.0, 0.0]},
                ],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            },
        )

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await adapter(client).embed(
                request_id="embed-contract",
                texts=("alpha", "beta"),
                model="embed-test",
                dimensions=3,
                timeout_seconds=2,
            )
        finally:
            await client.aclose()

    result = asyncio.run(exercise())

    assert captured["url"] == "https://api.openai.com/v1/embeddings"
    assert captured["headers"]["authorization"] == "Bearer test-embedding-key"
    assert captured["body"] == {
        "model": "embed-test",
        "input": ["alpha", "beta"],
        "encoding_format": "float",
        "dimensions": 3,
    }
    assert result.vectors == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    assert result.input_tokens == 7


def test_embedding_model_and_dimensions_fail_closed_before_network():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        instance = adapter(client)
        try:
            with pytest.raises(ProviderError) as model_error:
                await instance.embed(
                    request_id="unknown-model",
                    texts=("alpha",),
                    model="unknown",
                    dimensions=3,
                    timeout_seconds=2,
                )
            with pytest.raises(ProviderError) as dimension_error:
                await instance.embed(
                    request_id="bad-dimensions",
                    texts=("alpha",),
                    model="embed-test",
                    dimensions=2,
                    timeout_seconds=2,
                )
        finally:
            await client.aclose()
        return model_error.value, dimension_error.value

    model_error, dimension_error = asyncio.run(exercise())

    assert model_error.code == "embedding_model_not_configured"
    assert dimension_error.code == "embedding_dimensions_not_configured"
    assert calls == 0


def test_embedding_rate_limit_is_retryable_without_upstream_body_leak():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="sensitive upstream provider details")

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ProviderError) as error:
                await adapter(client).embed(
                    request_id="rate-limit",
                    texts=("alpha",),
                    model="embed-test",
                    dimensions=3,
                    timeout_seconds=2,
                )
        finally:
            await client.aclose()
        return error.value

    error = asyncio.run(exercise())

    assert error.code == "embedding_rate_limited"
    assert error.retryable is True
    assert "sensitive upstream provider details" not in str(error)


def test_embedding_adapter_rejects_malformed_or_zero_vectors():
    responses = iter(
        [
            {
                "model": "embed-test",
                "data": [{"index": 0, "embedding": [0.0, 0.0, 0.0]}],
                "usage": {"prompt_tokens": 1},
            },
            {
                "model": "embed-test",
                "data": [{"index": 0, "embedding": [1.0, 0.0]}],
                "usage": {"prompt_tokens": 1},
            },
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async def exercise():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        instance = adapter(client)
        try:
            with pytest.raises(ProviderError) as zero:
                await instance.embed(
                    request_id="zero-vector",
                    texts=("alpha",),
                    model="embed-test",
                    dimensions=3,
                    timeout_seconds=2,
                )
            with pytest.raises(ProviderError) as dimensions:
                await instance.embed(
                    request_id="wrong-vector-dim",
                    texts=("alpha",),
                    model="embed-test",
                    dimensions=3,
                    timeout_seconds=2,
                )
        finally:
            await client.aclose()
        return zero.value, dimensions.value

    zero, dimensions = asyncio.run(exercise())

    assert zero.code == "embedding_zero_vector"
    assert dimensions.code == "embedding_dimension_mismatch"


def test_embedding_cancel_interrupts_active_request():
    async def exercise():
        started = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        instance = adapter(client)
        task = asyncio.create_task(
            instance.embed(
                request_id="cancel-embedding",
                texts=("alpha",),
                model="embed-test",
                dimensions=3,
                timeout_seconds=30,
            )
        )
        await started.wait()
        await instance.cancel("cancel-embedding")
        try:
            with pytest.raises(ProviderError) as error:
                await task
        finally:
            await client.aclose()
        return error.value

    error = asyncio.run(exercise())

    assert error.code == "embedding_cancelled"
    assert error.retryable is False
