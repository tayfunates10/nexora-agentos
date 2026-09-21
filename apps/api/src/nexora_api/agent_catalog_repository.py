"""Persistence for the standard agent catalog and its staged rollouts.

The invariants this module enforces are what make an agent releasable on its own clock:

* A published version is immutable. Re-publishing the same number is a conflict, never an
  overwrite, so a tenant pinned to 1.4.2 always gets exactly the 1.4.2 that was reviewed.
* Versions only move forward. A new release must sort above every existing one.
* Exactly one rollout is active per agent and channel, and a rollback restores the version
  the rollout replaced rather than deleting history.
"""

from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from nexora_api.agent_catalog import (
    CHANNEL_SCOPE,
    OFFERABLE_STATUSES,
    CatalogAgent,
    CatalogAgentInput,
    CatalogAgentUpdate,
    CatalogVersion,
    CatalogVersionSummary,
    Channel,
    Entitlement,
    Rollout,
    RolloutInput,
    RolloutState,
    RolloutUpdate,
    VersionReleaseInput,
)
from nexora_api.agent_manifest import AgentManifest, Version
from nexora_api.auth import Principal
from nexora_api.platform_admin import PlatformRepository, rollout_bucket

AGENT_FIELDS = "id,slug,name,description,category,icon,status,visibility,created_at,updated_at"
VERSION_FIELDS = (
    "id,catalog_agent_id,version,status,channel,min_runtime_version,changelog,manifest,created_at"
)
AGENT_COLUMNS = ",".join("a." + field for field in AGENT_FIELDS.split(","))
VERSION_COLUMNS = ",".join("v." + field for field in VERSION_FIELDS.split(","))


class AgentCatalogRepository(PlatformRepository):
    @staticmethod
    def _agent(row) -> CatalogAgent:
        return CatalogAgent(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
            category=row["category"],
            icon=row["icon"],
            status=row["status"],
            visibility=row["visibility"],
            latest_version=row.get("latest_version"),
            version_count=row.get("version_count") or 0,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _version(row) -> CatalogVersion:
        return CatalogVersion(
            id=row["id"],
            catalog_agent_id=row["catalog_agent_id"],
            version=row["version"],
            status=row["status"],
            channel=row["channel"],
            min_runtime_version=row["min_runtime_version"],
            changelog=row["changelog"],
            manifest=AgentManifest.model_validate(row["manifest"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _summary(row) -> CatalogVersionSummary:
        return CatalogVersionSummary(
            id=row["id"],
            version=row["version"],
            status=row["status"],
            channel=row["channel"],
            min_runtime_version=row["min_runtime_version"],
            changelog=row["changelog"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _rollout(row) -> Rollout:
        return Rollout(
            id=row["id"],
            catalog_agent_id=row["catalog_agent_id"],
            version_id=row["version_id"],
            version=row["version"],
            channel=row["channel"],
            percentage=row["percentage"],
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------ catalog entries
    async def create_agent(
        self, principal: Principal, body: CatalogAgentInput, request_id: str
    ) -> CatalogAgent:
        async with self.connection() as connection:
            try:
                result = await connection.execute(
                    f"""INSERT INTO catalog_agents
                        (id,slug,name,description,category,icon,status,visibility,
                         created_by_issuer,created_by_subject)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING {AGENT_FIELDS}""",
                    (
                        uuid4(),
                        body.slug,
                        body.name,
                        body.description,
                        body.category,
                        body.icon,
                        body.status,
                        body.visibility,
                        principal.issuer,
                        principal.subject,
                    ),
                )
            except psycopg.errors.UniqueViolation:
                raise HTTPException(409, "A catalog agent with this slug already exists") from None
            row = await result.fetchone()
            await self.audit(connection, principal, "catalog.agent.created", request_id, body.slug)
            return self._agent(row)

    async def list_agents(self, limit: int, cursor: str | None) -> list[CatalogAgent]:
        async with self.connection() as connection:
            result = await connection.execute(
                f"""SELECT {AGENT_COLUMNS},
                       (SELECT count(*) FROM catalog_agent_versions v
                        WHERE v.catalog_agent_id=a.id) AS version_count,
                       (SELECT v.version FROM catalog_agent_versions v
                        WHERE v.catalog_agent_id=a.id
                        ORDER BY v.major DESC,v.minor DESC,v.patch DESC LIMIT 1) AS latest_version
                    FROM catalog_agents a
                    WHERE (%s::text IS NULL OR a.slug > %s::text)
                    ORDER BY a.slug LIMIT %s""",
                (cursor, cursor, limit),
            )
            return [self._agent(row) for row in await result.fetchall()]

    async def _agent_row(self, connection, slug: str, lock: bool = False):
        result = await connection.execute(
            f"""SELECT {AGENT_COLUMNS},
                   (SELECT count(*) FROM catalog_agent_versions v
                    WHERE v.catalog_agent_id=a.id) AS version_count,
                   (SELECT v.version FROM catalog_agent_versions v
                    WHERE v.catalog_agent_id=a.id
                    ORDER BY v.major DESC,v.minor DESC,v.patch DESC LIMIT 1) AS latest_version
                FROM catalog_agents a WHERE a.slug=%s"""
            + (" FOR UPDATE OF a" if lock else ""),
            (slug,),
        )
        row = await result.fetchone()
        if row is None:
            raise HTTPException(404)
        return row

    async def get_agent(self, slug: str) -> CatalogAgent:
        async with self.connection() as connection:
            return self._agent(await self._agent_row(connection, slug))

    async def update_agent(
        self, principal: Principal, slug: str, body: CatalogAgentUpdate, request_id: str
    ) -> CatalogAgent:
        changes = body.model_dump(exclude_none=True)
        async with self.connection() as connection:
            await self._agent_row(connection, slug, lock=True)
            assignments = ",".join(f"{field}=%s" for field in changes)
            await connection.execute(
                f"UPDATE catalog_agents SET {assignments},updated_at=now() WHERE slug=%s",
                (*changes.values(), slug),
            )
            await self.audit(
                connection,
                principal,
                "catalog.agent.updated",
                request_id,
                slug,
                {"fields": sorted(changes)},
            )
            return self._agent(await self._agent_row(connection, slug))

    # ---------------------------------------------------------------------- versions
    async def publish_version(
        self, principal: Principal, slug: str, manifest: AgentManifest, request_id: str
    ) -> CatalogVersion:
        if manifest.slug != slug:
            raise HTTPException(422, "Manifest slug does not match the catalog entry")
        async with self.connection() as connection:
            agent = await self._agent_row(connection, slug, lock=True)
            released = manifest.semver
            highest = await connection.execute(
                """SELECT version FROM catalog_agent_versions WHERE catalog_agent_id=%s
                   ORDER BY major DESC,minor DESC,patch DESC LIMIT 1""",
                (agent["id"],),
            )
            previous = await highest.fetchone()
            if previous and Version(previous["version"]) >= released:
                raise HTTPException(
                    409,
                    "A published version never changes; publish a higher semantic version",
                )
            version_id = uuid4()
            result = await connection.execute(
                f"""INSERT INTO catalog_agent_versions
                    (id,catalog_agent_id,version,major,minor,patch,status,channel,manifest,
                     min_runtime_version,changelog,published_by_issuer,published_by_subject)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING {VERSION_FIELDS}""",
                (
                    version_id,
                    agent["id"],
                    manifest.version,
                    released.major,
                    released.minor,
                    released.patch,
                    manifest.status,
                    manifest.channel,
                    Jsonb(manifest.model_dump(mode="json")),
                    manifest.min_runtime_version,
                    manifest.changelog,
                    principal.issuer,
                    principal.subject,
                ),
            )
            row = await result.fetchone()
            await self.audit(
                connection,
                principal,
                "catalog.version.published",
                request_id,
                f"{slug}@{manifest.version}",
                {
                    "channel": manifest.channel,
                    "status": manifest.status,
                    "bump": released.bump_kind(Version(previous["version"]))
                    if previous
                    else "initial",
                },
            )
            return self._version(row)

    async def list_versions(
        self, slug: str, limit: int, cursor: str | None
    ) -> list[CatalogVersionSummary]:
        async with self.connection() as connection:
            agent = await self._agent_row(connection, slug)
            # Paging walks downwards through semantic order, so the cursor is the last
            # version already shown and the comparison is on the numeric triple.
            major, minor, patch = Version(cursor).parts if cursor else (None, None, None)
            result = await connection.execute(
                """SELECT id,version,status,channel,min_runtime_version,changelog,created_at
                   FROM catalog_agent_versions
                   WHERE catalog_agent_id=%s
                     AND (%s::int IS NULL
                          OR (major,minor,patch) < (%s::int,%s::int,%s::int))
                   ORDER BY major DESC,minor DESC,patch DESC LIMIT %s""",
                (agent["id"], major, major, minor, patch, limit),
            )
            return [self._summary(row) for row in await result.fetchall()]

    async def _version_row(self, connection, slug: str, version: str, lock: bool = False):
        result = await connection.execute(
            f"""SELECT {VERSION_COLUMNS} FROM catalog_agent_versions v
                JOIN catalog_agents a ON a.id=v.catalog_agent_id
                WHERE a.slug=%s AND v.version=%s"""
            + (" FOR UPDATE OF v" if lock else ""),
            (slug, version),
        )
        row = await result.fetchone()
        if row is None:
            raise HTTPException(404)
        return row

    async def get_version(self, slug: str, version: str) -> CatalogVersion:
        async with self.connection() as connection:
            return self._version(await self._version_row(connection, slug, version))

    async def set_version_release(
        self,
        principal: Principal,
        slug: str,
        version: str,
        body: VersionReleaseInput,
        request_id: str,
    ) -> CatalogVersion:
        """Move a published version between statuses and channels. The manifest is untouched."""
        changes = body.model_dump(exclude_none=True)
        async with self.connection() as connection:
            await self._version_row(connection, slug, version, lock=True)
            assignments = ",".join(f"{field}=%s" for field in changes)
            await connection.execute(
                f"""UPDATE catalog_agent_versions SET {assignments}
                    WHERE version=%s AND catalog_agent_id=(SELECT id FROM catalog_agents
                                                           WHERE slug=%s)""",
                (*changes.values(), version, slug),
            )
            await self.audit(
                connection,
                principal,
                "catalog.version.release_changed",
                request_id,
                f"{slug}@{version}",
                changes,
            )
            return self._version(await self._version_row(connection, slug, version))

    # ---------------------------------------------------------------------- rollouts
    async def create_rollout(
        self, principal: Principal, slug: str, body: RolloutInput, request_id: str
    ) -> Rollout:
        async with self.connection() as connection:
            agent = await self._agent_row(connection, slug, lock=True)
            version = await self._version_row(connection, slug, body.version)
            if version["status"] not in OFFERABLE_STATUSES:
                raise HTTPException(409, "Only a stable or beta version can be rolled out")
            # Superseding, not deleting: the previous rollout stays in history as completed.
            await connection.execute(
                """UPDATE agent_rollouts SET state='completed',updated_at=now()
                   WHERE catalog_agent_id=%s AND channel=%s AND state='active'""",
                (agent["id"], body.channel),
            )
            rollout_id = uuid4()
            await connection.execute(
                """INSERT INTO agent_rollouts
                   (id,catalog_agent_id,version_id,channel,percentage,state,
                    created_by_issuer,created_by_subject)
                   VALUES (%s,%s,%s,%s,%s,'active',%s,%s)""",
                (
                    rollout_id,
                    agent["id"],
                    version["id"],
                    body.channel,
                    body.percentage,
                    principal.issuer,
                    principal.subject,
                ),
            )
            await self.audit(
                connection,
                principal,
                "catalog.rollout.created",
                request_id,
                f"{slug}@{body.version}",
                {"channel": body.channel, "percentage": body.percentage},
            )
            return await self._rollout_by_id(connection, agent["id"], rollout_id)

    async def _rollout_by_id(self, connection, catalog_agent_id: UUID, rollout_id: UUID) -> Rollout:
        result = await connection.execute(
            """SELECT r.id,r.catalog_agent_id,r.version_id,v.version,r.channel,r.percentage,
                      r.state,r.created_at,r.updated_at
               FROM agent_rollouts r JOIN catalog_agent_versions v ON v.id=r.version_id
               WHERE r.id=%s AND r.catalog_agent_id=%s""",
            (rollout_id, catalog_agent_id),
        )
        row = await result.fetchone()
        if row is None:
            raise HTTPException(404)
        return self._rollout(row)

    async def list_rollouts(self, slug: str, limit: int, cursor: UUID | None) -> list[Rollout]:
        async with self.connection() as connection:
            agent = await self._agent_row(connection, slug)
            result = await connection.execute(
                """SELECT r.id,r.catalog_agent_id,r.version_id,v.version,r.channel,r.percentage,
                          r.state,r.created_at,r.updated_at
                   FROM agent_rollouts r JOIN catalog_agent_versions v ON v.id=r.version_id
                   WHERE r.catalog_agent_id=%s AND (%s::uuid IS NULL OR r.id > %s::uuid)
                   ORDER BY r.id LIMIT %s""",
                (agent["id"], cursor, cursor, limit),
            )
            return [self._rollout(row) for row in await result.fetchall()]

    async def update_rollout(
        self,
        principal: Principal,
        slug: str,
        rollout_id: UUID,
        body: RolloutUpdate,
        request_id: str,
    ) -> Rollout:
        changes = body.model_dump(exclude_none=True)
        async with self.connection() as connection:
            agent = await self._agent_row(connection, slug, lock=True)
            current = await self._rollout_by_id(connection, agent["id"], rollout_id)
            if current.state in (RolloutState.ROLLED_BACK, RolloutState.COMPLETED):
                raise HTTPException(409, "This rollout has already finished")
            assignments = ",".join(f"{field}=%s" for field in changes)
            await connection.execute(
                f"UPDATE agent_rollouts SET {assignments},updated_at=now() WHERE id=%s",
                (*changes.values(), rollout_id),
            )
            await self.audit(
                connection,
                principal,
                "catalog.rollout.updated",
                request_id,
                f"{slug}@{current.version}",
                changes,
            )
            return await self._rollout_by_id(connection, agent["id"], rollout_id)

    async def rollback_rollout(
        self, principal: Principal, slug: str, rollout_id: UUID, request_id: str
    ) -> Rollout:
        """Retire a rollout and reinstate the version that was serving the channel before it."""
        async with self.connection() as connection:
            agent = await self._agent_row(connection, slug, lock=True)
            current = await self._rollout_by_id(connection, agent["id"], rollout_id)
            if current.state is RolloutState.ROLLED_BACK:
                raise HTTPException(409, "This rollout was already rolled back")
            previous = await connection.execute(
                """SELECT r.version_id,v.version FROM agent_rollouts r
                   JOIN catalog_agent_versions v ON v.id=r.version_id
                   WHERE r.catalog_agent_id=%s AND r.channel=%s AND r.created_at<%s
                     AND r.state<>'rolled_back'
                   ORDER BY r.created_at DESC,r.id DESC LIMIT 1""",
                (agent["id"], current.channel, current.created_at),
            )
            restored = await previous.fetchone()
            await connection.execute(
                "UPDATE agent_rollouts SET state='rolled_back',updated_at=now() WHERE id=%s",
                (rollout_id,),
            )
            if restored is None:
                # Nothing preceded it, so the channel simply has no rollout again. Tenants
                # fall back to the newest version their channel offers.
                await self.audit(
                    connection,
                    principal,
                    "catalog.rollout.rolled_back",
                    request_id,
                    f"{slug}@{current.version}",
                    {"restored": None, "channel": current.channel},
                )
                return await self._rollout_by_id(connection, agent["id"], rollout_id)

            replacement = uuid4()
            await connection.execute(
                """INSERT INTO agent_rollouts
                   (id,catalog_agent_id,version_id,channel,percentage,state,
                    created_by_issuer,created_by_subject)
                   VALUES (%s,%s,%s,%s,100,'active',%s,%s)""",
                (
                    replacement,
                    agent["id"],
                    restored["version_id"],
                    current.channel,
                    principal.issuer,
                    principal.subject,
                ),
            )
            await self.audit(
                connection,
                principal,
                "catalog.rollout.rolled_back",
                request_id,
                f"{slug}@{current.version}",
                {"restored": restored["version"], "channel": current.channel},
            )
            return await self._rollout_by_id(connection, agent["id"], replacement)

    # ------------------------------------------------------------------ entitlements
    async def grant_entitlement(
        self, principal: Principal, slug: str, workspace_id: UUID, request_id: str
    ) -> Entitlement:
        async with self.connection() as connection:
            agent = await self._agent_row(connection, slug)
            try:
                result = await connection.execute(
                    """INSERT INTO catalog_agent_entitlements
                       (catalog_agent_id,workspace_id,granted_by_issuer,granted_by_subject)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (catalog_agent_id,workspace_id) DO UPDATE
                           SET granted_by_subject=EXCLUDED.granted_by_subject
                       RETURNING catalog_agent_id,workspace_id,created_at""",
                    (agent["id"], workspace_id, principal.issuer, principal.subject),
                )
            except psycopg.errors.ForeignKeyViolation:
                raise HTTPException(404, "Unknown workspace") from None
            row = await result.fetchone()
            await self.audit(
                connection,
                principal,
                "catalog.entitlement.granted",
                request_id,
                slug,
                {"workspace_id": str(workspace_id)},
            )
            return Entitlement(**row)

    async def revoke_entitlement(
        self, principal: Principal, slug: str, workspace_id: UUID, request_id: str
    ) -> None:
        async with self.connection() as connection:
            agent = await self._agent_row(connection, slug)
            await connection.execute(
                """DELETE FROM catalog_agent_entitlements
                   WHERE catalog_agent_id=%s AND workspace_id=%s""",
                (agent["id"], workspace_id),
            )
            await self.audit(
                connection,
                principal,
                "catalog.entitlement.revoked",
                request_id,
                slug,
                {"workspace_id": str(workspace_id)},
            )

    async def list_entitlements(
        self, slug: str, limit: int, cursor: UUID | None
    ) -> list[Entitlement]:
        async with self.connection() as connection:
            agent = await self._agent_row(connection, slug)
            result = await connection.execute(
                """SELECT catalog_agent_id,workspace_id,created_at
                   FROM catalog_agent_entitlements
                   WHERE catalog_agent_id=%s AND (%s::uuid IS NULL OR workspace_id > %s::uuid)
                   ORDER BY workspace_id LIMIT %s""",
                (agent["id"], cursor, cursor, limit),
            )
            return [Entitlement(**row) for row in await result.fetchall()]


async def offered_version(connection, catalog_agent_id: UUID, workspace_id: UUID, channel: Channel):
    """The version this workspace is currently entitled to run on its channel.

    A staged rollout narrows what a workspace sees: a tenant outside the rollout's bucket
    keeps the version that was serving the channel before, rather than jumping ahead. With
    no active rollout the newest offerable version in the channel wins.
    """
    scope = list(CHANNEL_SCOPE[Channel(channel)])
    active = await connection.execute(
        """SELECT r.version_id,r.percentage,v.version,v.major,v.minor,v.patch
           FROM agent_rollouts r JOIN catalog_agent_versions v ON v.id=r.version_id
           WHERE r.catalog_agent_id=%s AND r.state='active' AND r.channel=ANY(%s)""",
        (catalog_agent_id, scope),
    )
    bucket = rollout_bucket(catalog_agent_id, workspace_id)
    withheld: list[UUID] = []
    included: list[dict] = []
    for row in await active.fetchall():
        if bucket < row["percentage"]:
            included.append(row)
        else:
            withheld.append(row["version_id"])
    if included:
        chosen = max(included, key=lambda row: (row["major"], row["minor"], row["patch"]))
        return chosen["version_id"], chosen["version"]

    result = await connection.execute(
        """SELECT id,version FROM catalog_agent_versions
           WHERE catalog_agent_id=%s AND channel=ANY(%s) AND status=ANY(%s)
             AND NOT (id = ANY(%s::uuid[]))
           ORDER BY major DESC,minor DESC,patch DESC LIMIT 1""",
        (catalog_agent_id, scope, list(OFFERABLE_STATUSES), withheld),
    )
    row = await result.fetchone()
    return (row["id"], row["version"]) if row else (None, None)
