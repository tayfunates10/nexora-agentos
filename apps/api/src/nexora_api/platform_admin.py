"""Platform administration: the identity that owns the standard agent catalog.

Workspace roles answer "what may this member do inside their tenant". They deliberately
say nothing about publishing a standard agent, because that acts on every tenant at once.

Platform administration is therefore deployment configuration, not a grantable role: the
operator lists the verified token subjects in the process environment. No API call, no
workspace membership and no agent action can add one, so a compromised tenant cannot
escalate into the catalog.
"""

import hashlib
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, HTTPException, Request
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nexora_api.auth import Principal, authenticated
from nexora_api.config import Settings

ROLLOUT_BUCKETS = 100


async def platform_admin(request: Request, principal: Annotated[Principal, Depends(authenticated)]):
    """Allow only operator-listed subjects, and never reveal that the surface exists."""
    settings: Settings = request.app.state.settings
    admins = settings.platform_admins
    # An unconfigured deployment has no platform administrators, not every administrator.
    if not admins or principal.subject not in admins:
        raise HTTPException(404)
    return principal


PlatformIdentity = Annotated[Principal, Depends(platform_admin)]


def rollout_bucket(catalog_agent_id: UUID, workspace_id: UUID) -> int:
    """A stable 0-99 bucket for staged rollouts.

    The bucket depends only on the agent and the workspace, so widening a rollout can add
    tenants but never moves one that was already included, and the same tenant lands in
    the same bucket in every process that computes it.
    """
    digest = hashlib.sha256(f"{catalog_agent_id}:{workspace_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % ROLLOUT_BUCKETS


class PlatformRepository:
    """Shared database plumbing and the append-only platform audit journal."""

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

    async def audit(
        self,
        connection,
        principal: Principal,
        action: str,
        request_id: str,
        target: str | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Record a catalog action. Detail carries identifiers and versions, never secrets."""
        await connection.execute(
            """INSERT INTO platform_events
               (id, actor_issuer, actor_subject, action, request_id, target, detail)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (
                uuid4(),
                principal.issuer,
                principal.subject,
                action,
                request_id,
                target,
                Jsonb(detail or {}),
            ),
        )
