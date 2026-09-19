import json
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row
from redis.asyncio import Redis

from nexora_api.config import Settings

QUEUE_KEY = "nexora:jobs:agent-runs"
MAX_ATTEMPTS = 10


class OutboxPublisher:
    def __init__(self, settings: Settings):
        self.settings = settings

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ) as connection:
            yield connection

    async def publish_batch(self, redis: Redis, limit: int = 50) -> int:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        published = 0
        async with self.connection() as connection:
            result = await connection.execute(
                """SELECT id,workspace_id,run_id,topic,payload,attempts
                   FROM job_outbox
                   WHERE published_at IS NULL AND dead_lettered_at IS NULL
                     AND available_at <= now()
                   ORDER BY created_at,id
                   FOR UPDATE SKIP LOCKED
                   LIMIT %s""",
                (limit,),
            )
            for row in await result.fetchall():
                message = json.dumps(
                    {
                        "job_id": str(row["id"]),
                        "workspace_id": str(row["workspace_id"]),
                        "run_id": str(row["run_id"]),
                        "topic": row["topic"],
                        "payload": row["payload"],
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                try:
                    await redis.rpush(QUEUE_KEY, message)
                except Exception as exc:
                    attempts = row["attempts"] + 1
                    if attempts >= MAX_ATTEMPTS:
                        await connection.execute(
                            """UPDATE job_outbox
                               SET attempts=%s,dead_lettered_at=now(),last_error=%s
                               WHERE id=%s""",
                            (attempts, type(exc).__name__[:100], row["id"]),
                        )
                    else:
                        delay = min(300, 2 ** min(attempts, 8))
                        await connection.execute(
                            """UPDATE job_outbox
                               SET attempts=%s,available_at=now()+(%s * interval '1 second'),
                                   last_error=%s
                               WHERE id=%s""",
                            (attempts, delay, type(exc).__name__[:100], row["id"]),
                        )
                else:
                    await connection.execute(
                        """UPDATE job_outbox
                           SET attempts=attempts+1,published_at=now(),last_error=NULL
                           WHERE id=%s""",
                        (row["id"],),
                    )
                    published += 1
        return published
