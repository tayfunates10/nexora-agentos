"""Distributed authenticated API rate limiting without storing tenant identity in Redis."""

import hashlib
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

_RATE_LIMIT_SCRIPT = """
local current = redis.call("INCR", KEYS[1])
if current == 1 then
  redis.call("EXPIRE", KEYS[1], ARGV[1])
end
local ttl = redis.call("TTL", KEYS[1])
if ttl < 0 then
  redis.call("EXPIRE", KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {current, ttl}
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimitUnavailable(RuntimeError):
    """The shared limiter could not make a trustworthy decision."""


class IdentityRateLimiter:
    def __init__(self, redis: Redis, limit: int, window_seconds: int) -> None:
        self.redis = redis
        self.limit = limit
        self.window_seconds = window_seconds

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @staticmethod
    def identity_key(issuer: str, subject: str) -> str:
        digest = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
        return f"nexora:rate-limit:identity:v1:{digest}"

    async def check(self, issuer: str, subject: str) -> RateLimitDecision:
        if not self.enabled:
            return RateLimitDecision(allowed=True, remaining=0, retry_after_seconds=0)
        key = self.identity_key(issuer, subject)
        try:
            current, ttl = await self.redis.eval(
                _RATE_LIMIT_SCRIPT, 1, key, self.window_seconds
            )
        except RedisError as exc:
            raise RateLimitUnavailable("Shared rate limiter is unavailable") from exc
        count = int(current)
        retry_after = max(1, int(ttl))
        return RateLimitDecision(
            allowed=count <= self.limit,
            remaining=max(0, self.limit - count),
            retry_after_seconds=retry_after,
        )
