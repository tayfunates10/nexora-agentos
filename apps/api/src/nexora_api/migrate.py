"""Explicit, forward-only migrations. Run before starting the API, never per request."""

import hashlib
from pathlib import Path

import psycopg

from nexora_api.config import Settings


def migrate(settings: Settings | None = None):
    settings = settings or Settings()
    with psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=5) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(71639201)")
        connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            name text PRIMARY KEY, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())""")
        for path in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
            contents = path.read_text()
            checksum = hashlib.sha256(contents.encode()).hexdigest()
            row = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE name=%s", (path.name,)
            ).fetchone()
            if row:
                if row[0] != checksum:
                    raise RuntimeError("Applied migration changed: " + path.name)
                continue
            connection.execute(contents)
            connection.execute(
                "INSERT INTO schema_migrations(name,checksum) VALUES (%s,%s)", (path.name, checksum)
            )


if __name__ == "__main__":
    migrate()
