import hashlib
import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nexora_api.agents import AgentDefinition, AgentInput, AgentRun, RunEvent, RunInput
from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.runtime_events import append_run_event
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission, authorize


class AgentRuntimeRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.workspaces = WorkspaceRepository(settings)

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(),
            connect_timeout=3,
            row_factory=dict_row,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ) as connection:
            yield connection

    @staticmethod
    def agent(row) -> AgentDefinition:
        return AgentDefinition(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            instructions=row["instructions"],
            model_profile=row["model_profile"],
            created_at=row["created_at"],
        )

    @staticmethod
    def run(row) -> AgentRun:
        return AgentRun(
            id=row["id"],
            workspace_id=row["workspace_id"],
            agent_id=row["agent_id"],
            trace_id=row["trace_id"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            cancel_requested_at=row["cancel_requested_at"],
            finished_at=row["finished_at"],
            failure_code=row["failure_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def create_agent(
        self, principal: Principal, workspace_id: UUID, body: AgentInput, request_id: str
    ) -> AgentDefinition:
        agent_id = uuid4()
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_AGENTS
            )
            result = await connection.execute(
                """INSERT INTO agent_definitions
                   (id,workspace_id,name,instructions,model_profile,created_by_issuer,
                    created_by_subject)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id,workspace_id,name,instructions,model_profile,created_at""",
                (
                    agent_id,
                    workspace_id,
                    body.name,
                    body.instructions,
                    body.model_profile,
                    principal.issuer,
                    principal.subject,
                ),
            )
            row = await result.fetchone()
            await self.workspaces.audit(
                connection, principal, workspace_id, "agent.created", request_id
            )
            return self.agent(row)

    async def list_agents(
        self, principal: Principal, workspace_id: UUID, limit: int, cursor: UUID | None
    ) -> list[AgentDefinition]:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            result = await connection.execute(
                """SELECT id,workspace_id,name,instructions,model_profile,created_at
                   FROM agent_definitions
                   WHERE workspace_id=%s AND (%s::uuid IS NULL OR id > %s::uuid)
                   ORDER BY id LIMIT %s""",
                (workspace_id, cursor, cursor, limit),
            )
            return [self.agent(row) for row in await result.fetchall()]

    async def get_agent(
        self, principal: Principal, workspace_id: UUID, agent_id: UUID
    ) -> AgentDefinition:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            result = await connection.execute(
                """SELECT id,workspace_id,name,instructions,model_profile,created_at
                   FROM agent_definitions WHERE workspace_id=%s AND id=%s""",
                (workspace_id, agent_id),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(404)
            return self.agent(row)

    async def create_run(
        self,
        principal: Principal,
        workspace_id: UUID,
        body: RunInput,
        idempotency_key: str,
        request_id: str,
    ) -> tuple[AgentRun, bool]:
        fingerprint = hashlib.sha256(
            json.dumps(
                {"agent_id": str(body.agent_id), "input": body.input},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.RUN_AGENTS)
            agent = await connection.execute(
                "SELECT id FROM agent_definitions WHERE workspace_id=%s AND id=%s",
                (workspace_id, body.agent_id),
            )
            if not await agent.fetchone():
                raise HTTPException(404)

            existing_result = await connection.execute(
                """SELECT id,workspace_id,agent_id,trace_id,status,attempt_count,
                          cancel_requested_at,finished_at,failure_code,created_at,updated_at,
                          request_hash
                   FROM agent_runs
                   WHERE workspace_id=%s AND requested_by_issuer=%s
                     AND requested_by_subject=%s AND idempotency_key=%s
                   FOR UPDATE""",
                (workspace_id, principal.issuer, principal.subject, idempotency_key),
            )
            existing = await existing_result.fetchone()
            if existing:
                if existing["request_hash"] != fingerprint:
                    raise HTTPException(409)
                return self.run(existing), False

            run_id = uuid4()
            trace_id = uuid4()
            result = await connection.execute(
                """INSERT INTO agent_runs
                   (id,workspace_id,agent_id,requested_by_issuer,requested_by_subject,
                    input_text,request_hash,idempotency_key,trace_id,status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued')
                   RETURNING id,workspace_id,agent_id,trace_id,status,attempt_count,
                             cancel_requested_at,finished_at,failure_code,created_at,updated_at""",
                (
                    run_id,
                    workspace_id,
                    body.agent_id,
                    principal.issuer,
                    principal.subject,
                    body.input,
                    fingerprint,
                    idempotency_key,
                    trace_id,
                ),
            )
            row = await result.fetchone()
            await connection.execute(
                """INSERT INTO agent_run_events
                   (id,workspace_id,run_id,event_no,event_type,payload)
                   VALUES (%s,%s,%s,1,'run.queued',%s)""",
                (uuid4(), workspace_id, run_id, Jsonb({"request_id": request_id})),
            )
            await connection.execute(
                """INSERT INTO job_outbox (id,workspace_id,run_id,topic,payload)
                   VALUES (%s,%s,%s,'agent.run.queued.v1',%s)""",
                (
                    uuid4(),
                    workspace_id,
                    run_id,
                    Jsonb(
                        {
                            "run_id": str(run_id),
                            "workspace_id": str(workspace_id),
                            "agent_id": str(body.agent_id),
                            "trace_id": str(trace_id),
                        }
                    ),
                ),
            )
            await self.workspaces.audit(
                connection, principal, workspace_id, "agent_run.created", request_id
            )
            return self.run(row), True

    async def get_run(self, principal: Principal, workspace_id: UUID, run_id: UUID) -> AgentRun:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            result = await connection.execute(
                """SELECT id,workspace_id,agent_id,trace_id,status,attempt_count,
                          cancel_requested_at,finished_at,failure_code,created_at,updated_at
                   FROM agent_runs WHERE workspace_id=%s AND id=%s""",
                (workspace_id, run_id),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(404)
            return self.run(row)

    async def cancel_run(self, principal, workspace_id, run_id, request_id):
        async with self.connection() as connection:
            membership = await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.READ
            )
            result = await connection.execute(
                "SELECT * FROM agent_runs WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (workspace_id, run_id),
            )
            run = await result.fetchone()
            if not run:
                raise HTTPException(404)
            is_requester = (
                run["requested_by_issuer"] == principal.issuer
                and run["requested_by_subject"] == principal.subject
            )
            if not is_requester:
                authorize(membership["role"], Permission.CANCEL_ANY_RUN)

            if run["status"] in ("succeeded", "failed", "cancelled"):
                return self.run(run)

            if run["status"] == "running":
                if run["cancel_requested_at"] is None:
                    updated = await connection.execute(
                        """UPDATE agent_runs
                           SET cancel_requested_at=now(),updated_at=now()
                           WHERE id=%s RETURNING *""",
                        (run_id,),
                    )
                    run = await updated.fetchone()
                    await append_run_event(
                        connection,
                        workspace_id,
                        run_id,
                        "run.cancel_requested",
                        {"request_id": request_id},
                    )
                    await self.workspaces.audit(
                        connection,
                        principal,
                        workspace_id,
                        "agent_run.cancel_requested",
                        request_id,
                    )
                return self.run(run)

            updated = await connection.execute(
                """UPDATE agent_runs
                   SET status='cancelled',cancel_requested_at=COALESCE(cancel_requested_at,now()),
                       finished_at=now(),lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                   WHERE id=%s RETURNING *""",
                (run_id,),
            )
            run = await updated.fetchone()
            await append_run_event(
                connection,
                workspace_id,
                run_id,
                "run.cancelled",
                {"request_id": request_id, "reason": "requested"},
            )
            await self.workspaces.audit(
                connection, principal, workspace_id, "agent_run.cancelled", request_id
            )
            return self.run(run)

    async def list_events(
        self,
        principal: Principal,
        workspace_id: UUID,
        run_id: UUID,
        limit: int,
        cursor: int,
    ) -> list[RunEvent]:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            run = await connection.execute(
                "SELECT id FROM agent_runs WHERE workspace_id=%s AND id=%s",
                (workspace_id, run_id),
            )
            if not await run.fetchone():
                raise HTTPException(404)
            result = await connection.execute(
                """SELECT id,event_no,event_type,payload,created_at
                   FROM agent_run_events
                   WHERE workspace_id=%s AND run_id=%s AND event_no>%s
                   ORDER BY event_no LIMIT %s""",
                (workspace_id, run_id, cursor, limit),
            )
            return [RunEvent(**row) for row in await result.fetchall()]
