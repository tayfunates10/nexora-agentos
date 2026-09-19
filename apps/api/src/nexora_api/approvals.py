from contextlib import asynccontextmanager
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.runtime_events import append_run_event
from nexora_api.tool_registry import ToolGatewayError, ToolRegistry
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    CANCEL = "cancel"


class ApprovalDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: ApprovalDecision
    reason: str | None = Field(default=None, min_length=1, max_length=500)


class ApprovalRecord(BaseModel):
    id: UUID
    run_id: UUID
    tool_call_id: UUID
    tool_name: str
    arguments: dict
    arguments_hash: str
    requested_by_subject: str
    status: str
    policy_reason: str
    requested_at: datetime
    expires_at: datetime
    decided_by_subject: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None


class ApprovalService:
    def __init__(self, settings: Settings, registry: ToolRegistry):
        self.settings = settings
        self.registry = registry
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
    def record(row):
        return ApprovalRecord(
            id=row["id"],
            run_id=row["run_id"],
            tool_call_id=row["tool_call_id"],
            tool_name=row["tool_name"],
            arguments=row["arguments"],
            arguments_hash=row["arguments_hash"],
            requested_by_subject=row["requested_by_subject"],
            status=row["status"],
            policy_reason=row["policy_reason"],
            requested_at=row["requested_at"],
            expires_at=row["expires_at"],
            decided_by_subject=row["decided_by_subject"],
            decided_at=row["decided_at"],
            decision_reason=row["decision_reason"],
        )

    async def list_pending(self, principal: Principal, workspace_id: UUID, limit: int = 50):
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.APPROVE_TOOLS
            )
            result = await connection.execute(
                """SELECT a.*,c.tool_name,c.arguments
                   FROM tool_approvals a JOIN tool_calls c ON c.id=a.tool_call_id
                   WHERE a.workspace_id=%s AND a.status='pending'
                   ORDER BY a.requested_at,a.id LIMIT %s""",
                (workspace_id, limit),
            )
            return [self.record(row) for row in await result.fetchall()]

    async def decide(
        self,
        principal: Principal,
        workspace_id: UUID,
        approval_id: UUID,
        body: ApprovalDecisionInput,
        request_id: str,
    ):
        async with self.connection() as connection:
            result = await connection.execute(
                """SELECT a.*,c.tool_name,c.arguments,c.status AS tool_status,
                          c.arguments_hash AS call_arguments_hash,r.status AS run_status
                   FROM tool_approvals a
                   JOIN tool_calls c ON c.id=a.tool_call_id
                   JOIN agent_runs r ON r.id=a.run_id AND r.workspace_id=a.workspace_id
                   WHERE a.id=%s AND a.workspace_id=%s
                   FOR UPDATE OF a,c,r""",
                (approval_id, workspace_id),
            )
            row = await result.fetchone()
            if not row:
                raise HTTPException(404)

            is_requester = (
                row["requested_by_issuer"] == principal.issuer
                and row["requested_by_subject"] == principal.subject
            )
            if body.decision == ApprovalDecision.CANCEL and is_requester:
                await self.workspaces.scoped(
                    connection, principal, workspace_id, Permission.READ
                )
            else:
                await self.workspaces.scoped(
                    connection, principal, workspace_id, Permission.APPROVE_TOOLS
                )

            if row["status"] != "pending":
                return self.record(row)

            now_result = await connection.execute("SELECT now() AS now")
            now = (await now_result.fetchone())["now"]
            if row["expires_at"] <= now:
                await self._finish(
                    connection,
                    row,
                    "expired",
                    principal,
                    body.reason or "approval_expired",
                    request_id,
                )
            else:
                self._revalidate_contract(row)
                target = {
                    ApprovalDecision.APPROVE: "approved",
                    ApprovalDecision.REJECT: "rejected",
                    ApprovalDecision.CANCEL: "cancelled",
                }[body.decision]
                await self._finish(
                    connection,
                    row,
                    target,
                    principal,
                    body.reason,
                    request_id,
                )

            refreshed = await connection.execute(
                """SELECT a.*,c.tool_name,c.arguments
                   FROM tool_approvals a JOIN tool_calls c ON c.id=a.tool_call_id
                   WHERE a.id=%s""",
                (approval_id,),
            )
            return self.record(await refreshed.fetchone())

    def _revalidate_contract(self, row):
        spec = self.registry.get(row["tool_name"])
        validated = self.registry.validate_arguments(spec, row["arguments"])
        from nexora_api.mcp_gateway import canonical_payload

        _, arguments_hash = canonical_payload(validated)
        if arguments_hash != row["arguments_hash"] or arguments_hash != row["call_arguments_hash"]:
            raise ToolGatewayError("approved_arguments_changed")

    async def _finish(
        self, connection, row, status, principal, reason, request_id
    ):
        if row["run_status"] == "cancelled":
            status = "cancelled"
        await connection.execute(
            """UPDATE tool_approvals
               SET status=%s,decided_by_issuer=%s,decided_by_subject=%s,
                   decided_at=now(),decision_reason=%s
               WHERE id=%s""",
            (status, principal.issuer, principal.subject, reason, row["id"]),
        )
        await connection.execute(
            """UPDATE tool_calls SET status=%s,updated_at=now(),
                   error_code=CASE WHEN %s='approved' THEN NULL ELSE %s END,
                   finished_at=CASE WHEN %s='approved' THEN NULL ELSE now() END
               WHERE id=%s""",
            (
                status,
                status,
                "approval_" + status,
                status,
                row["tool_call_id"],
            ),
        )
        if row["run_status"] == "waiting_for_approval":
            await connection.execute(
                """UPDATE agent_runs SET status='queued',updated_at=now()
                   WHERE id=%s""",
                (row["run_id"],),
            )
            run_result = await connection.execute(
                """SELECT agent_id,trace_id FROM agent_runs WHERE id=%s""",
                (row["run_id"],),
            )
            run = await run_result.fetchone()
            await connection.execute(
                """INSERT INTO job_outbox (id,workspace_id,run_id,topic,payload)
                   VALUES (%s,%s,%s,'agent.run.queued.v1',%s)""",
                (
                    uuid4(),
                    row["workspace_id"],
                    row["run_id"],
                    Jsonb(
                        {
                            "run_id": str(row["run_id"]),
                            "workspace_id": str(row["workspace_id"]),
                            "agent_id": str(run["agent_id"]),
                            "trace_id": str(run["trace_id"]),
                        }
                    ),
                ),
            )
            await append_run_event(
                connection,
                row["workspace_id"],
                row["run_id"],
                "tool.approval_" + status,
                {
                    "tool_call_id": str(row["tool_call_id"]),
                    "approval_id": str(row["id"]),
                },
            )
            await append_run_event(
                connection,
                row["workspace_id"],
                row["run_id"],
                "run.resumed",
                {"approval_id": str(row["id"]), "decision": status},
            )
        await self.workspaces.audit(
            connection,
            principal,
            row["workspace_id"],
            "tool_approval." + status,
            request_id,
        )

    async def expire_pending(self, limit: int = 100):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        expired = 0
        async with self.connection() as connection:
            result = await connection.execute(
                """SELECT a.*,c.tool_name,c.arguments,c.arguments_hash AS call_arguments_hash,
                          r.status AS run_status
                   FROM tool_approvals a
                   JOIN tool_calls c ON c.id=a.tool_call_id
                   JOIN agent_runs r ON r.id=a.run_id AND r.workspace_id=a.workspace_id
                   WHERE a.status='pending' AND a.expires_at <= now()
                   ORDER BY a.expires_at,a.id
                   FOR UPDATE OF a,c,r SKIP LOCKED
                   LIMIT %s""",
                (limit,),
            )
            for row in await result.fetchall():
                system = Principal("nexora:system", "approval-expirer")
                await self._finish(
                    connection,
                    row,
                    "expired",
                    system,
                    "approval_expired",
                    "approval-expirer",
                )
                expired += 1
        return expired
