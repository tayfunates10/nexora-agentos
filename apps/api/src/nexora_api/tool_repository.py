import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.run_state import ExecutionContext
from nexora_api.runtime_events import append_run_event
from nexora_api.tool_contracts import (
    ToolContractError,
    hash_tool_contract,
    validate_arguments,
    validate_registration_schema,
    validate_result,
)
from nexora_api.tooling import (
    ToolApproval,
    ToolDefinition,
    ToolPolicy,
    ToolPolicyInput,
    ToolSideEffect,
    ToolUpsertInput,
)
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission, authorize

_CALL_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_FORCE_APPROVAL = {
    ToolSideEffect.DESTRUCTIVE.value,
    ToolSideEffect.EXTERNAL_COMMUNICATION.value,
}


@dataclass(frozen=True)
class ToolCallPlan:
    action: Literal["execute", "approval", "deny", "replay"]
    call_id: UUID
    tool: ToolDefinition
    approval_id: UUID | None = None
    result: Any = None
    error_code: str | None = None


@dataclass(frozen=True)
class ToolExecution:
    call_id: UUID
    server_key: str
    remote_name: str
    arguments: dict[str, Any]
    output_schema: dict[str, Any] | None


class ToolGovernanceRepository:
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
    def _tool(row) -> ToolDefinition:
        return ToolDefinition(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            server_key=row["server_key"],
            remote_name=row["remote_name"],
            description=row["description"],
            input_schema=row["input_schema"],
            output_schema=row["output_schema"],
            side_effect=row["side_effect"],
            enabled=row["enabled"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _policy(row) -> ToolPolicy:
        return ToolPolicy(
            workspace_id=row["workspace_id"],
            tool_id=row["tool_id"],
            decision=row["decision"],
            reason=row["reason"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _approval(row) -> ToolApproval:
        return ToolApproval(
            id=row["id"],
            workspace_id=row["workspace_id"],
            run_id=row["run_id"],
            tool_call_id=row["tool_call_id"],
            requested_action=row["requested_action"],
            normalized_arguments=row["normalized_arguments"],
            requester_subject=row["requester_subject"],
            approver_subject=row["approver_subject"],
            status=row["status"],
            policy_reason=row["policy_reason"],
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )

    async def upsert_tool(
        self,
        principal: Principal,
        workspace_id: UUID,
        name: str,
        body: ToolUpsertInput,
        request_id: str,
    ) -> ToolDefinition:
        validate_registration_schema(
            body.input_schema,
            side_effect=body.side_effect.value,
        )
        if body.output_schema is not None:
            validate_registration_schema(body.output_schema, output=True)

        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_TOOLS
            )
            result = await connection.execute(
                """INSERT INTO tool_definitions
                   (id,workspace_id,name,server_key,remote_name,description,input_schema,
                    output_schema,side_effect,enabled,created_by_issuer,created_by_subject)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (workspace_id,name) DO UPDATE SET
                     server_key=EXCLUDED.server_key,
                     remote_name=EXCLUDED.remote_name,
                     description=EXCLUDED.description,
                     input_schema=EXCLUDED.input_schema,
                     output_schema=EXCLUDED.output_schema,
                     side_effect=EXCLUDED.side_effect,
                     enabled=EXCLUDED.enabled,
                     updated_at=now()
                   RETURNING *""",
                (
                    uuid4(),
                    workspace_id,
                    name,
                    body.server_key,
                    body.remote_name,
                    body.description,
                    Jsonb(body.input_schema),
                    Jsonb(body.output_schema) if body.output_schema is not None else None,
                    body.side_effect.value,
                    body.enabled,
                    principal.issuer,
                    principal.subject,
                ),
            )
            row = await result.fetchone()
            await self.workspaces.audit(
                connection,
                principal,
                workspace_id,
                "tool.upserted",
                request_id,
                name,
            )
            return self._tool(row)

    async def list_tools(
        self,
        principal: Principal,
        workspace_id: UUID,
        limit: int,
        cursor: str | None,
    ) -> list[ToolDefinition]:
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            result = await connection.execute(
                """SELECT * FROM tool_definitions
                   WHERE workspace_id=%s AND (%s::text IS NULL OR name > %s::text)
                   ORDER BY name LIMIT %s""",
                (workspace_id, cursor, cursor, limit),
            )
            return [self._tool(row) for row in await result.fetchall()]

    async def set_policy(
        self,
        principal: Principal,
        workspace_id: UUID,
        tool_name: str,
        body: ToolPolicyInput,
        request_id: str,
    ) -> ToolPolicy:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_TOOLS
            )
            tool_result = await connection.execute(
                """SELECT id FROM tool_definitions
                   WHERE workspace_id=%s AND name=%s FOR UPDATE""",
                (workspace_id, tool_name),
            )
            tool = await tool_result.fetchone()
            if not tool:
                raise HTTPException(404)
            result = await connection.execute(
                """INSERT INTO tool_policies
                   (workspace_id,tool_id,decision,reason,updated_by_issuer,updated_by_subject)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (workspace_id,tool_id) DO UPDATE SET
                     decision=EXCLUDED.decision,
                     reason=EXCLUDED.reason,
                     updated_by_issuer=EXCLUDED.updated_by_issuer,
                     updated_by_subject=EXCLUDED.updated_by_subject,
                     updated_at=now()
                   RETURNING *""",
                (
                    workspace_id,
                    tool["id"],
                    body.decision.value,
                    body.reason,
                    principal.issuer,
                    principal.subject,
                ),
            )
            row = await result.fetchone()
            await self.workspaces.audit(
                connection,
                principal,
                workspace_id,
                "tool.policy_set." + body.decision.value,
                request_id,
                tool_name,
            )
            return self._policy(row)

    async def list_approvals(
        self,
        principal: Principal,
        workspace_id: UUID,
        limit: int,
        cursor: UUID | None,
    ) -> list[ToolApproval]:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.APPROVE_TOOLS
            )
            await self._expire_due(connection, workspace_id)
            result = await connection.execute(
                """SELECT * FROM tool_approvals
                   WHERE workspace_id=%s AND (%s::uuid IS NULL OR id > %s::uuid)
                   ORDER BY id LIMIT %s""",
                (workspace_id, cursor, cursor, limit),
            )
            return [self._approval(row) for row in await result.fetchall()]

    async def decide_approval(
        self,
        principal: Principal,
        workspace_id: UUID,
        approval_id: UUID,
        decision: str,
        request_id: str,
    ) -> ToolApproval:
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.APPROVE_TOOLS
            )
            result = await connection.execute(
                """SELECT a.*,c.tool_id,c.arguments,c.contract_hash AS call_contract_hash,
                          t.server_key,t.remote_name,t.input_schema,t.output_schema,
                          t.enabled,t.side_effect,p.decision AS current_policy
                   FROM tool_approvals a
                   JOIN tool_calls c ON c.id=a.tool_call_id
                   JOIN tool_definitions t ON t.id=c.tool_id AND t.workspace_id=c.workspace_id
                   LEFT JOIN tool_policies p
                     ON p.tool_id=t.id AND p.workspace_id=t.workspace_id
                   WHERE a.workspace_id=%s AND a.id=%s
                   FOR UPDATE OF a,c,t""",
                (workspace_id, approval_id),
            )
            approval = await result.fetchone()
            if not approval:
                raise HTTPException(404)
            if approval["status"] != "pending":
                return self._approval(approval)

            now_result = await connection.execute("SELECT now() AS now")
            now = (await now_result.fetchone())["now"]
            if approval["expires_at"] <= now:
                await self._cancel_approval(
                    connection, approval, "expired", "tool.approval_expired"
                )
                refreshed = await self._approval_row(connection, approval_id)
                return self._approval(refreshed)

            run_result = await connection.execute(
                "SELECT * FROM agent_runs WHERE id=%s AND workspace_id=%s FOR UPDATE",
                (approval["run_id"], workspace_id),
            )
            run = await run_result.fetchone()
            if not run or run["status"] != "waiting_for_approval":
                raise HTTPException(409)

            if decision == "rejected":
                await connection.execute(
                    """UPDATE tool_approvals
                       SET status='rejected',approver_issuer=%s,approver_subject=%s,
                           decided_at=now()
                       WHERE id=%s""",
                    (principal.issuer, principal.subject, approval_id),
                )
                await connection.execute(
                    """UPDATE tool_calls
                       SET status='denied',updated_at=now(),finished_at=now()
                       WHERE id=%s""",
                    (approval["tool_call_id"],),
                )
                await self._cancel_run(
                    connection,
                    run,
                    "tool.rejected",
                    {"approval_id": str(approval_id)},
                )
                await self.workspaces.audit(
                    connection,
                    principal,
                    workspace_id,
                    "tool.approval_rejected",
                    request_id,
                    str(approval["tool_call_id"]),
                )
                refreshed = await self._approval_row(connection, approval_id)
                return self._approval(refreshed)

            current_contract_hash = hash_tool_contract(
                server_key=approval["server_key"],
                remote_name=approval["remote_name"],
                input_schema=approval["input_schema"],
                output_schema=approval["output_schema"],
                side_effect=approval["side_effect"],
            )
            if (
                approval["contract_hash"] != approval["call_contract_hash"]
                or approval["contract_hash"] != current_contract_hash
            ):
                await self._cancel_approval(
                    connection,
                    approval,
                    "cancelled",
                    "tool.approval_invalidated",
                )
                refreshed = await self._approval_row(connection, approval_id)
                return self._approval(refreshed)

            requester = await connection.execute(
                """SELECT role FROM workspace_memberships
                   WHERE workspace_id=%s AND issuer=%s AND subject=%s""",
                (
                    workspace_id,
                    approval["requester_issuer"],
                    approval["requester_subject"],
                ),
            )
            membership = await requester.fetchone()
            try:
                authorize(membership["role"] if membership else None, Permission.RUN_AGENTS)
                validate_arguments(approval["arguments"], approval["input_schema"])
            except (HTTPException, ToolContractError):
                await self._cancel_approval(
                    connection,
                    approval,
                    "cancelled",
                    "tool.approval_invalidated",
                )
                refreshed = await self._approval_row(connection, approval_id)
                return self._approval(refreshed)

            effective = self._effective_policy(approval["current_policy"], approval["side_effect"])
            if not approval["enabled"] or effective == "deny":
                await self._cancel_approval(
                    connection,
                    approval,
                    "cancelled",
                    "tool.approval_invalidated",
                )
                refreshed = await self._approval_row(connection, approval_id)
                return self._approval(refreshed)

            await connection.execute(
                """UPDATE tool_approvals
                   SET status='approved',approver_issuer=%s,approver_subject=%s,decided_at=now()
                   WHERE id=%s""",
                (principal.issuer, principal.subject, approval_id),
            )
            await connection.execute(
                "UPDATE tool_calls SET status='approved',updated_at=now() WHERE id=%s",
                (approval["tool_call_id"],),
            )
            await connection.execute(
                """UPDATE agent_runs
                   SET status='queued',lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                   WHERE id=%s""",
                (run["id"],),
            )
            await append_run_event(
                connection,
                workspace_id,
                run["id"],
                "tool.approved",
                {
                    "tool_call_id": str(approval["tool_call_id"]),
                    "approval_id": str(approval_id),
                },
            )
            await append_run_event(
                connection,
                workspace_id,
                run["id"],
                "run.resumed",
                {"approval_id": str(approval_id)},
            )
            await self._enqueue_run(connection, run)
            await self.workspaces.audit(
                connection,
                principal,
                workspace_id,
                "tool.approval_approved",
                request_id,
                str(approval["tool_call_id"]),
            )
            refreshed = await self._approval_row(connection, approval_id)
            return self._approval(refreshed)

    async def prepare_call(
        self,
        context: ExecutionContext,
        call_key: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolCallPlan:
        if not _CALL_KEY.fullmatch(call_key):
            raise ToolContractError("invalid_call_key")

        async with self.connection() as connection:
            run_result = await connection.execute(
                """SELECT * FROM agent_runs
                   WHERE id=%s AND workspace_id=%s FOR UPDATE""",
                (context.run_id, context.workspace_id),
            )
            run = await run_result.fetchone()
            if not run:
                raise ToolContractError("run_not_executable")
            if not await self._has_active_execution_lease(connection, context, run):
                raise ToolContractError("run_lease_lost")

            membership_result = await connection.execute(
                """SELECT role FROM workspace_memberships
                   WHERE workspace_id=%s AND issuer=%s AND subject=%s""",
                (
                    context.workspace_id,
                    run["requested_by_issuer"],
                    run["requested_by_subject"],
                ),
            )
            membership = await membership_result.fetchone()
            try:
                authorize(membership["role"] if membership else None, Permission.RUN_AGENTS)
            except HTTPException:
                return await self._denied_plan(
                    connection, run, call_key, tool_name, arguments, "requester_not_authorized"
                )

            tool_result = await connection.execute(
                """SELECT t.*,p.decision AS policy_decision,p.reason AS policy_reason
                   FROM tool_definitions t
                   LEFT JOIN tool_policies p
                     ON p.tool_id=t.id AND p.workspace_id=t.workspace_id
                   WHERE t.workspace_id=%s AND t.name=%s
                   FOR UPDATE OF t""",
                (context.workspace_id, tool_name),
            )
            tool_row = await tool_result.fetchone()
            if not tool_row:
                raise ToolContractError("unknown_tool")
            tool = self._tool(tool_row)
            _, arguments_hash = validate_arguments(arguments, tool_row["input_schema"])
            contract_hash = hash_tool_contract(
                server_key=tool_row["server_key"],
                remote_name=tool_row["remote_name"],
                input_schema=tool_row["input_schema"],
                output_schema=tool_row["output_schema"],
                side_effect=tool_row["side_effect"],
            )

            effective = self._effective_policy(tool_row["policy_decision"], tool_row["side_effect"])
            if not tool_row["enabled"]:
                effective = "deny"
            reason = tool_row["policy_reason"] or "No explicit allow policy is configured."

            existing_result = await connection.execute(
                "SELECT * FROM tool_calls WHERE run_id=%s AND call_key=%s FOR UPDATE",
                (context.run_id, call_key),
            )
            existing = await existing_result.fetchone()
            if existing:
                if existing["tool_id"] != tool.id or existing["arguments_hash"] != arguments_hash:
                    raise ToolContractError("call_key_conflict")
                if existing["status"] == "succeeded":
                    return ToolCallPlan("replay", existing["id"], tool, result=existing["result"])
                if existing["contract_hash"] != contract_hash:
                    approval = await self._approval_for_call(connection, existing["id"])
                    if approval and approval["status"] == "pending":
                        await connection.execute(
                            """UPDATE tool_approvals
                               SET status='cancelled',decided_at=now()
                               WHERE id=%s""",
                            (approval["id"],),
                        )
                    if existing["status"] in (
                        "planned",
                        "pending_approval",
                        "approved",
                        "running",
                    ):
                        await connection.execute(
                            """UPDATE tool_calls
                               SET status='cancelled',error_code='tool_contract_changed',
                                   finished_at=now(),updated_at=now()
                               WHERE id=%s""",
                            (existing["id"],),
                        )
                    await append_run_event(
                        connection,
                        context.workspace_id,
                        context.run_id,
                        "tool.contract_changed",
                        {"tool": tool_name, "call_key": call_key},
                    )
                    return ToolCallPlan(
                        "deny",
                        existing["id"],
                        tool,
                        error_code="tool_contract_changed",
                    )
                if existing["status"] in ("failed", "denied", "cancelled"):
                    return ToolCallPlan(
                        "deny",
                        existing["id"],
                        tool,
                        error_code=existing["error_code"] or "tool_call_terminal",
                    )
                approval = await self._approval_for_call(connection, existing["id"])
                if approval and approval["status"] == "pending":
                    return ToolCallPlan(
                        "approval",
                        existing["id"],
                        tool,
                        approval_id=approval["id"],
                    )
                if effective == "deny":
                    target = (
                        "cancelled" if existing["status"] in ("approved", "running") else "denied"
                    )
                    await connection.execute(
                        """UPDATE tool_calls
                           SET status=%s,error_code='policy_denied',
                               updated_at=now(),finished_at=now()
                           WHERE id=%s""",
                        (target, existing["id"]),
                    )
                    await append_run_event(
                        connection,
                        context.workspace_id,
                        context.run_id,
                        "tool.denied",
                        {"tool": tool_name, "call_key": call_key},
                    )
                    return ToolCallPlan("deny", existing["id"], tool, error_code="policy_denied")
                if effective == "require_approval" and not (
                    approval and approval["status"] == "approved"
                ):
                    if existing["status"] != "planned":
                        return ToolCallPlan(
                            "deny",
                            existing["id"],
                            tool,
                            error_code="approval_required_after_execution_started",
                        )
                    return await self._create_approval(
                        connection, run, existing["id"], tool, arguments, arguments_hash, reason
                    )
                return ToolCallPlan("execute", existing["id"], tool)

            if effective == "deny":
                call_id = uuid4()
                await connection.execute(
                    """INSERT INTO tool_calls
                       (id,workspace_id,run_id,tool_id,call_key,arguments,arguments_hash,
                        contract_hash,status,policy_decision,error_code,finished_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                               'denied','deny','policy_denied',now())""",
                    (
                        call_id,
                        context.workspace_id,
                        context.run_id,
                        tool.id,
                        call_key,
                        Jsonb(arguments),
                        arguments_hash,
                        contract_hash,
                    ),
                )
                await append_run_event(
                    connection,
                    context.workspace_id,
                    context.run_id,
                    "tool.denied",
                    {"tool": tool_name, "call_key": call_key},
                )
                return ToolCallPlan("deny", call_id, tool, error_code="policy_denied")

            call_id = uuid4()
            initial_status = "pending_approval" if effective == "require_approval" else "planned"
            await connection.execute(
                """INSERT INTO tool_calls
                   (id,workspace_id,run_id,tool_id,call_key,arguments,arguments_hash,
                    contract_hash,status,policy_decision)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    call_id,
                    context.workspace_id,
                    context.run_id,
                    tool.id,
                    call_key,
                    Jsonb(arguments),
                    arguments_hash,
                    contract_hash,
                    initial_status,
                    effective,
                ),
            )
            await append_run_event(
                connection,
                context.workspace_id,
                context.run_id,
                "tool.call_planned",
                {
                    "tool": tool_name,
                    "call_key": call_key,
                    "side_effect": tool.side_effect.value,
                    "policy_decision": effective,
                },
            )
            if effective == "require_approval":
                return await self._create_approval(
                    connection, run, call_id, tool, arguments, arguments_hash, reason
                )
            return ToolCallPlan("execute", call_id, tool)

    async def mark_running(
        self,
        context: ExecutionContext,
        call_id: UUID,
    ) -> ToolExecution:
        async with self.connection() as connection:
            result = await connection.execute(
                """SELECT c.*,t.server_key,t.remote_name,t.input_schema,t.output_schema,
                          t.side_effect,t.enabled,p.decision AS current_policy
                   FROM tool_calls c
                   JOIN tool_definitions t
                     ON t.id=c.tool_id AND t.workspace_id=c.workspace_id
                   LEFT JOIN tool_policies p
                     ON p.tool_id=t.id AND p.workspace_id=t.workspace_id
                   WHERE c.id=%s
                   FOR UPDATE OF c,t""",
                (call_id,),
            )
            call = await result.fetchone()
            if not call:
                raise ToolContractError("unknown_tool_call")
            run_result = await connection.execute(
                "SELECT * FROM agent_runs WHERE id=%s AND workspace_id=%s FOR UPDATE",
                (call["run_id"], call["workspace_id"]),
            )
            run = await run_result.fetchone()
            if not run:
                raise ToolContractError("run_not_executable")
            if not await self._has_active_execution_lease(connection, context, run):
                raise ToolContractError("run_lease_lost")

            membership_result = await connection.execute(
                """SELECT role FROM workspace_memberships
                   WHERE workspace_id=%s AND issuer=%s AND subject=%s""",
                (
                    call["workspace_id"],
                    run["requested_by_issuer"],
                    run["requested_by_subject"],
                ),
            )
            membership = await membership_result.fetchone()
            try:
                authorize(membership["role"] if membership else None, Permission.RUN_AGENTS)
            except HTTPException as exc:
                raise ToolContractError("requester_not_authorized") from exc

            if not call["enabled"]:
                raise ToolContractError("tool_disabled")
            current_contract_hash = hash_tool_contract(
                server_key=call["server_key"],
                remote_name=call["remote_name"],
                input_schema=call["input_schema"],
                output_schema=call["output_schema"],
                side_effect=call["side_effect"],
            )
            if call["contract_hash"] != current_contract_hash:
                raise ToolContractError("tool_contract_changed")
            effective = self._effective_policy(call["current_policy"], call["side_effect"])
            if effective == "deny":
                raise ToolContractError("policy_denied")

            validate_arguments(call["arguments"], call["input_schema"])
            if effective == "require_approval":
                approval = await self._approval_for_call(connection, call_id)
                if (
                    not approval
                    or approval["status"] != "approved"
                    or approval["arguments_hash"] != call["arguments_hash"]
                    or approval["contract_hash"] != call["contract_hash"]
                    or approval["normalized_arguments"] != call["arguments"]
                ):
                    raise ToolContractError("approval_missing_or_stale")

            if call["status"] not in ("planned", "approved", "running"):
                raise ToolContractError("tool_call_not_executable")
            await connection.execute(
                """UPDATE tool_calls
                   SET status='running',attempt_count=attempt_count+1,
                       started_at=COALESCE(started_at,now()),
                       updated_at=now(),error_code=NULL
                   WHERE id=%s""",
                (call_id,),
            )
            await append_run_event(
                connection,
                call["workspace_id"],
                call["run_id"],
                "tool.started",
                {"tool_call_id": str(call_id), "attempt": call["attempt_count"] + 1},
            )
            return ToolExecution(
                call_id=call_id,
                server_key=call["server_key"],
                remote_name=call["remote_name"],
                arguments=call["arguments"],
                output_schema=call["output_schema"],
            )

    async def complete_success(
        self,
        context: ExecutionContext,
        call_id: UUID,
        result_value: Any,
    ) -> bool:
        async with self.connection() as connection:
            row_result = await connection.execute(
                """SELECT c.*,t.output_schema FROM tool_calls c
                   JOIN tool_definitions t
                     ON t.id=c.tool_id AND t.workspace_id=c.workspace_id
                   WHERE c.id=%s FOR UPDATE OF c""",
                (call_id,),
            )
            row = await row_result.fetchone()
            if not row or row["status"] != "running":
                return False
            run_result = await connection.execute(
                """SELECT * FROM agent_runs
                   WHERE id=%s AND workspace_id=%s
                   FOR UPDATE""",
                (row["run_id"], row["workspace_id"]),
            )
            run = await run_result.fetchone()
            if not run or not await self._has_active_execution_lease(connection, context, run):
                return False
            validate_result(result_value, row["output_schema"])
            await connection.execute(
                """UPDATE tool_calls
                   SET status='succeeded',result=%s,error_code=NULL,
                       finished_at=now(),updated_at=now()
                   WHERE id=%s""",
                (Jsonb(result_value), call_id),
            )
            await append_run_event(
                connection,
                row["workspace_id"],
                row["run_id"],
                "tool.succeeded",
                {"tool_call_id": str(call_id)},
            )
            return True

    async def complete_failure(
        self,
        context: ExecutionContext,
        call_id: UUID,
        error_code: str,
        *,
        retryable: bool,
    ) -> bool:
        if not error_code or len(error_code) > 100:
            raise ValueError("invalid error_code")
        async with self.connection() as connection:
            row_result = await connection.execute(
                "SELECT * FROM tool_calls WHERE id=%s FOR UPDATE",
                (call_id,),
            )
            row = await row_result.fetchone()
            if not row or row["status"] != "running":
                return False
            run_result = await connection.execute(
                """SELECT * FROM agent_runs
                   WHERE id=%s AND workspace_id=%s
                   FOR UPDATE""",
                (row["run_id"], row["workspace_id"]),
            )
            run = await run_result.fetchone()
            if not run or not await self._has_active_execution_lease(connection, context, run):
                return False
            if retryable:
                approval = await self._approval_for_call(connection, call_id)
                next_status = (
                    "approved" if approval and approval["status"] == "approved" else "planned"
                )
                await connection.execute(
                    """UPDATE tool_calls
                       SET status=%s,error_code=%s,updated_at=now()
                       WHERE id=%s""",
                    (next_status, error_code, call_id),
                )
                event_type = "tool.retry_scheduled"
            else:
                await connection.execute(
                    """UPDATE tool_calls
                       SET status='failed',error_code=%s,finished_at=now(),updated_at=now()
                       WHERE id=%s""",
                    (error_code, call_id),
                )
                event_type = "tool.failed"
            await append_run_event(
                connection,
                row["workspace_id"],
                row["run_id"],
                event_type,
                {"tool_call_id": str(call_id), "error_code": error_code},
            )
            return True

    async def _create_approval(
        self,
        connection,
        run,
        call_id: UUID,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        arguments_hash: str,
        reason: str,
    ) -> ToolCallPlan:
        approval_id = uuid4()
        call_result = await connection.execute(
            "SELECT contract_hash FROM tool_calls WHERE id=%s",
            (call_id,),
        )
        call = await call_result.fetchone()
        if not call:
            raise ToolContractError("unknown_tool_call")
        await connection.execute(
            """UPDATE tool_calls
               SET status='pending_approval',policy_decision='require_approval',updated_at=now()
               WHERE id=%s""",
            (call_id,),
        )
        await connection.execute(
            """INSERT INTO tool_approvals
               (id,workspace_id,run_id,tool_call_id,requested_action,normalized_arguments,
                arguments_hash,contract_hash,requester_issuer,requester_subject,status,
                policy_reason,expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,
                       now()+interval '15 minutes')""",
            (
                approval_id,
                run["workspace_id"],
                run["id"],
                call_id,
                tool.name,
                Jsonb(arguments),
                arguments_hash,
                call["contract_hash"],
                run["requested_by_issuer"],
                run["requested_by_subject"],
                reason,
            ),
        )
        await append_run_event(
            connection,
            run["workspace_id"],
            run["id"],
            "tool.approval_requested",
            {
                "tool": tool.name,
                "tool_call_id": str(call_id),
                "approval_id": str(approval_id),
            },
        )
        return ToolCallPlan("approval", call_id, tool, approval_id=approval_id)

    async def _denied_plan(
        self,
        connection,
        run,
        call_key: str,
        tool_name: str,
        arguments: dict[str, Any],
        error_code: str,
    ) -> ToolCallPlan:
        tool_result = await connection.execute(
            "SELECT * FROM tool_definitions WHERE workspace_id=%s AND name=%s",
            (run["workspace_id"], tool_name),
        )
        tool_row = await tool_result.fetchone()
        if not tool_row:
            raise ToolContractError("unknown_tool")
        tool = self._tool(tool_row)
        _, digest = validate_arguments(arguments, tool_row["input_schema"])
        contract_hash = hash_tool_contract(
            server_key=tool_row["server_key"],
            remote_name=tool_row["remote_name"],
            input_schema=tool_row["input_schema"],
            output_schema=tool_row["output_schema"],
            side_effect=tool_row["side_effect"],
        )
        existing_result = await connection.execute(
            "SELECT * FROM tool_calls WHERE run_id=%s AND call_key=%s FOR UPDATE",
            (run["id"], call_key),
        )
        existing = await existing_result.fetchone()
        if existing:
            return ToolCallPlan("deny", existing["id"], tool, error_code=error_code)
        call_id = uuid4()
        await connection.execute(
            """INSERT INTO tool_calls
               (id,workspace_id,run_id,tool_id,call_key,arguments,arguments_hash,
                contract_hash,status,policy_decision,error_code,finished_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'denied','deny',%s,now())""",
            (
                call_id,
                run["workspace_id"],
                run["id"],
                tool.id,
                call_key,
                Jsonb(arguments),
                digest,
                contract_hash,
                error_code,
            ),
        )
        await append_run_event(
            connection,
            run["workspace_id"],
            run["id"],
            "tool.denied",
            {"tool": tool_name, "call_key": call_key, "reason": error_code},
        )
        return ToolCallPlan("deny", call_id, tool, error_code=error_code)

    async def _has_active_execution_lease(
        self,
        connection,
        context: ExecutionContext,
        run,
    ) -> bool:
        if (
            run["id"] != context.run_id
            or run["workspace_id"] != context.workspace_id
            or run["agent_id"] != context.agent_id
            or run["trace_id"] != context.trace_id
            or run["status"] != "running"
            or run["lease_owner"] != context.worker_id
            or run["lease_expires_at"] is None
        ):
            return False

        now_result = await connection.execute("SELECT now() AS now")
        now = (await now_result.fetchone())["now"]
        if run["lease_expires_at"] <= now:
            return False

        receipt_result = await connection.execute(
            """SELECT * FROM worker_job_receipts
               WHERE job_id=%s AND run_id=%s AND workspace_id=%s
               FOR UPDATE""",
            (context.job_id, context.run_id, context.workspace_id),
        )
        receipt = await receipt_result.fetchone()
        return bool(
            receipt
            and receipt["status"] == "processing"
            and receipt["worker_id"] == context.worker_id
            and receipt["lease_expires_at"] is not None
            and receipt["lease_expires_at"] > now
        )

    async def _approval_for_call(self, connection, call_id: UUID):
        result = await connection.execute(
            "SELECT * FROM tool_approvals WHERE tool_call_id=%s FOR UPDATE",
            (call_id,),
        )
        return await result.fetchone()

    async def _approval_row(self, connection, approval_id: UUID):
        result = await connection.execute(
            "SELECT * FROM tool_approvals WHERE id=%s",
            (approval_id,),
        )
        return await result.fetchone()

    @staticmethod
    def _effective_policy(decision: str | None, side_effect: str) -> str:
        if decision is None or decision == "deny":
            return "deny"
        if side_effect in _FORCE_APPROVAL:
            return "require_approval"
        return decision

    async def expire_due(self, limit: int = 100) -> int:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        async with self.connection() as connection:
            result = await connection.execute(
                """SELECT * FROM tool_approvals
                   WHERE status='pending' AND expires_at <= now()
                   ORDER BY expires_at,id
                   FOR UPDATE SKIP LOCKED
                   LIMIT %s""",
                (limit,),
            )
            approvals = await result.fetchall()
            for approval in approvals:
                await self._cancel_approval(
                    connection,
                    approval,
                    "expired",
                    "tool.approval_expired",
                )
            return len(approvals)

    async def _expire_due(self, connection, workspace_id: UUID) -> None:
        result = await connection.execute(
            """SELECT a.* FROM tool_approvals a
               WHERE a.workspace_id=%s AND a.status='pending' AND a.expires_at <= now()
               ORDER BY a.expires_at,a.id
               FOR UPDATE SKIP LOCKED""",
            (workspace_id,),
        )
        for approval in await result.fetchall():
            await self._cancel_approval(connection, approval, "expired", "tool.approval_expired")

    async def _cancel_approval(
        self,
        connection,
        approval,
        approval_status: str,
        event_type: str,
    ) -> None:
        await connection.execute(
            """UPDATE tool_approvals
               SET status=%s,decided_at=now()
               WHERE id=%s AND status='pending'""",
            (approval_status, approval["id"]),
        )
        await connection.execute(
            """UPDATE tool_calls
               SET status='cancelled',error_code=%s,finished_at=now(),updated_at=now()
               WHERE id=%s AND status IN ('planned','pending_approval','approved','running')""",
            (approval_status, approval["tool_call_id"]),
        )
        run_result = await connection.execute(
            "SELECT * FROM agent_runs WHERE id=%s AND workspace_id=%s FOR UPDATE",
            (approval["run_id"], approval["workspace_id"]),
        )
        run = await run_result.fetchone()
        if run and run["status"] in ("running", "waiting_for_approval"):
            await self._cancel_run(
                connection,
                run,
                event_type,
                {
                    "approval_id": str(approval["id"]),
                    "tool_call_id": str(approval["tool_call_id"]),
                },
            )

    async def _cancel_run(self, connection, run, event_type: str, payload: dict[str, object]):
        await connection.execute(
            """UPDATE agent_runs
               SET status='cancelled',finished_at=now(),lease_owner=NULL,
                   lease_expires_at=NULL,updated_at=now()
               WHERE id=%s""",
            (run["id"],),
        )
        await append_run_event(
            connection,
            run["workspace_id"],
            run["id"],
            event_type,
            payload,
        )
        await append_run_event(
            connection,
            run["workspace_id"],
            run["id"],
            "run.cancelled",
            {"reason": event_type},
        )

    async def _enqueue_run(self, connection, run) -> None:
        await connection.execute(
            """INSERT INTO job_outbox (id,workspace_id,run_id,topic,payload)
               VALUES (%s,%s,%s,'agent.run.queued.v1',%s)""",
            (
                uuid4(),
                run["workspace_id"],
                run["id"],
                Jsonb(
                    {
                        "run_id": str(run["id"]),
                        "workspace_id": str(run["workspace_id"]),
                        "agent_id": str(run["agent_id"]),
                        "trace_id": str(run["trace_id"]),
                    }
                ),
            ),
        )
