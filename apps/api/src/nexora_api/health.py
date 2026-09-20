import asyncio
from typing import Literal, Protocol

import psycopg
from pydantic import BaseModel
from redis.asyncio import Redis

from nexora_api.config import Settings


class DependencyStatus(BaseModel):
    postgres: Literal["up", "down"]
    redis: Literal["up", "down"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    # The worker serves its own probes; a probe must not claim to be the API.
    service: Literal["nexora-api", "nexora-worker"] = "nexora-api"
    version: str = "0.1.0"
    dependencies: DependencyStatus | None = None


class Probe(Protocol):
    async def check(self) -> DependencyStatus: ...


class DependencyProbe:
    def __init__(self, settings: Settings, redis: Redis):
        self.settings = settings
        self.redis = redis

    async def _postgres(self) -> None:
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(), connect_timeout=2
        ) as connection:
            # Readiness requires the vector extension, not only an open socket.
            cursor = await connection.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            if await cursor.fetchone() is None:
                raise RuntimeError("Required extension unavailable")

    async def _redis(self) -> None:
        if not await self.redis.ping():
            raise RuntimeError("Redis unavailable")

    async def _safe(self, check) -> Literal["up", "down"]:
        try:
            async with asyncio.timeout(self.settings.dependency_timeout_seconds):
                await check()
            return "up"
        except Exception:
            # Never expose connection strings, credentials, or driver error text.
            return "down"

    async def check(self) -> DependencyStatus:
        postgres, redis = await asyncio.gather(self._safe(self._postgres), self._safe(self._redis))
        return DependencyStatus(postgres=postgres, redis=redis)
