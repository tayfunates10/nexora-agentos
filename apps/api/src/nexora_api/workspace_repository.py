from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.workspaces import Membership, Permission, Role, Workspace, authorize


class WorkspaceRepository:
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

    async def audit(self, connection, principal, workspace_id, action, request_id, target=None):
        await connection.execute(
            """INSERT INTO security_events
               (id, workspace_id, actor_issuer, actor_subject, action, request_id, target_subject)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (
                uuid4(),
                workspace_id,
                principal.issuer,
                principal.subject,
                action,
                request_id,
                target,
            ),
        )

    async def scoped(self, connection, principal, workspace_id, permission):
        # Row lock serializes role changes with privileged writes in the same transaction.
        cursor = await connection.execute(
            """SELECT w.id, w.name, m.role FROM workspaces w
               JOIN workspace_memberships m ON m.workspace_id=w.id
               WHERE w.id=%s AND m.issuer=%s AND m.subject=%s FOR UPDATE OF w, m""",
            (workspace_id, principal.issuer, principal.subject),
        )
        row = await cursor.fetchone()
        authorize(row["role"] if row else None, permission)
        return row

    async def create(self, principal: Principal, name: str, request_id: str) -> Workspace:
        workspace_id = uuid4()
        async with self.connection() as connection:
            await connection.execute(
                "INSERT INTO workspaces (id,name) VALUES (%s,%s)", (workspace_id, name)
            )
            await connection.execute(
                """INSERT INTO workspace_memberships (workspace_id,issuer,subject,role)
                   VALUES (%s,%s,%s,'owner')""",
                (workspace_id, principal.issuer, principal.subject),
            )
            await self.audit(connection, principal, workspace_id, "workspace.created", request_id)
        return Workspace(id=workspace_id, name=name, role=Role.OWNER)

    async def list(self, principal: Principal, limit: int, cursor: UUID | None):
        async with self.connection() as connection:
            result = await connection.execute(
                """SELECT w.id,w.name,m.role FROM workspaces w
                   JOIN workspace_memberships m ON m.workspace_id=w.id
                   WHERE m.issuer=%s AND m.subject=%s
                   AND (%s::uuid IS NULL OR w.id > %s::uuid)
                   ORDER BY w.id LIMIT %s""",
                (principal.issuer, principal.subject, cursor, cursor, limit),
            )
            return [Workspace(**row) for row in await result.fetchall()]

    async def get(self, principal, workspace_id):
        async with self.connection() as connection:
            row = await self.scoped(connection, principal, workspace_id, Permission.READ)
            return Workspace(**row)

    async def rename(self, principal, workspace_id, name, request_id):
        async with self.connection() as connection:
            row = await self.scoped(connection, principal, workspace_id, Permission.UPDATE)
            await connection.execute(
                "UPDATE workspaces SET name=%s WHERE id=%s", (name, workspace_id)
            )
            await self.audit(connection, principal, workspace_id, "workspace.renamed", request_id)
            return Workspace(id=workspace_id, name=name, role=row["role"])

    async def set_member(self, principal, workspace_id, subject, role, request_id):
        async with self.connection() as connection:
            await self.scoped(connection, principal, workspace_id, Permission.MANAGE_MEMBERS)
            if role == Role.OWNER:
                raise HTTPException(403)
            current = await connection.execute(
                """SELECT role FROM workspace_memberships
                   WHERE workspace_id=%s AND issuer=%s AND subject=%s""",
                (workspace_id, principal.issuer, subject),
            )
            existing = await current.fetchone()
            if existing and existing["role"] == Role.OWNER:
                raise HTTPException(403)
            await connection.execute(
                """INSERT INTO workspace_memberships (workspace_id,issuer,subject,role)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (workspace_id,issuer,subject) DO UPDATE SET role=EXCLUDED.role""",
                (workspace_id, principal.issuer, subject, role),
            )
            await self.audit(
                connection, principal, workspace_id, "membership.set." + role, request_id, subject
            )
            return Membership(subject=subject, role=role)
