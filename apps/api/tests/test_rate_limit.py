import asyncio
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError

from nexora_api.config import Settings
from nexora_api.rate_limit import IdentityRateLimiter, RateLimitUnavailable


class StubRedis:
    def __init__(self, result=(1, 60), error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def eval(self, script, key_count, key, window):
        self.calls.append((script, key_count, key, window))
        if self.error:
            raise self.error
        return self.result


def run(coro):
    return asyncio.run(coro)


def test_disabled_limiter_never_touches_redis():
    redis = StubRedis()
    limiter = IdentityRateLimiter(redis, limit=0, window_seconds=60)
    decision = run(limiter.check("https://issuer.example", "alice"))
    assert decision.allowed is True
    assert redis.calls == []


def test_identity_key_is_stable_and_does_not_store_subject():
    first = IdentityRateLimiter.identity_key("https://issuer.example", "alice@example.test")
    second = IdentityRateLimiter.identity_key("https://issuer.example", "alice@example.test")
    other = IdentityRateLimiter.identity_key("https://issuer.example", "bob@example.test")
    assert first == second
    assert first != other
    assert "alice" not in first
    assert "example.test" not in first


def test_limit_decision_uses_atomic_counter_result_and_ttl():
    redis = StubRedis(result=(3, 17))
    limiter = IdentityRateLimiter(redis, limit=2, window_seconds=60)
    decision = run(limiter.check("issuer", "alice"))
    assert decision.allowed is False
    assert decision.remaining == 0
    assert decision.retry_after_seconds == 17
    assert redis.calls[0][1] == 1
    assert redis.calls[0][3] == 60


def test_redis_failure_never_fails_open():
    redis = StubRedis(error=ConnectionError("offline"))
    limiter = IdentityRateLimiter(redis, limit=2, window_seconds=60)
    with pytest.raises(RateLimitUnavailable):
        run(limiter.check("issuer", "alice"))


@pytest.mark.integration
def test_two_limiter_instances_share_the_same_redis_counter():
    settings = Settings()
    subject = f"rate-limit-{uuid4()}"

    async def scenario():
        redis_one = Redis.from_url(settings.redis_url.get_secret_value())
        redis_two = Redis.from_url(settings.redis_url.get_secret_value())
        try:
            first = IdentityRateLimiter(redis_one, limit=2, window_seconds=60)
            second = IdentityRateLimiter(redis_two, limit=2, window_seconds=60)
            assert (await first.check("integration-issuer", subject)).allowed is True
            assert (await second.check("integration-issuer", subject)).allowed is True
            decision = await first.check("integration-issuer", subject)
            assert decision.allowed is False
            assert 1 <= decision.retry_after_seconds <= 60
        finally:
            await redis_one.aclose()
            await redis_two.aclose()

    run(scenario())
