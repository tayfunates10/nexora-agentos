import hashlib
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from nexora_api.auth import Principal
from nexora_api.config import Settings
from nexora_api.run_state import ExecutionContext
from nexora_api.runtime_events import append_run_event
from nexora_api.tool_policy import evaluate_policy
from nexora_api.tool_registry import (
    PolicyDecision,
    ToolApprovalRequired,
    ToolExecutionContext,
    ToolExecutionError,
    ToolGatewayError,
    ToolRegistry,
)
from nexora_api.workspace_repository import WorkspaceRepository
from nexora_api.workspaces import Permission


@dataclass(frozen=True)
class ToolCallOutcome:
    tool_call_id: UUID
    status: str
    output: dict | None = None
    error_code: str | None = None


def canonical_payload(model: BaseModel) -> tuple[dict, str]:
    payload = model.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return payload, hashlib.sha256(encoded).hexdigest()


class McpGateway:
    """Transport-neutral MCP tool boundary. Wire transports map onto this service."""

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

    async def list_tools(self, principal: Principal, workspace_id: UUID):
        async with self.connection() as connection:
            await self.workspaces.scoped(connection, principal, workspace_id, Permission.READ)
            items = []
            for spec in self.registry.list():
                policy = await evaluate_policy(connection, workspace_id, spec)
                items.append({**spec.contract(), "effective_policy": policy.decision})
            return items

    async def set_policy(
        self,
        principal: Principal,
        workspace_id: UUID,
        tool_name: str,
        decision: PolicyDecision,
        request_id: str,
    ):
        spec = self.registry.get(tool_name)
        async with self.connection() as connection:
            await self.workspaces.scoped(
                connection, principal, workspace_id, Permission.MANAGE_TOOL_POLICY
            )
            await connection.execute(
                """INSERT INTO workspace_tool_policies
                   (workspace_id,tool_name,decision,updated_by_issuer,updated_by_subject)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (workspace_id,tool_name) DO UPDATE SET
                     decision=EXCLUDED.decision,
                     updated_by_issuer=EXCLUDED.updated_by_issuer,
                     updated_by_subject=EXCLUDED.updated_by_subject,
                     updated_at=now()""",
                (
                    workspace_id,
                    spec.name,
                    decision,
                    principal.issuer,
                    principal.subject,
                ),
            )
            await self.workspaces.audit(
                connection,
                principal,
                workspace_id,
                "tool_policy.set." + decision,
                request_id,
            )
        return {"tool_name": spec.name, "decision": decision}

    async def call_tool(
        self,
        context: ExecutionContext,
        tool_name: str,
        arguments: dict,
        idempotency_key: str,
    ) -> ToolCallOutcome:
        if not 8 <= len(idempotency_key) <= 128:
            raise ToolGatewayError("invalid_idempotency_key")
        spec = self.registry.get(tool_name)
        validated = self.registry.validate_arguments(spec, arguments)
        normalized, arguments_hash = canonical_payload(validated)
        tool_call_id: UUID | None = None
        approval_id: UUID | None = None
        approved_before = False
        actor: Principal | None = None

        async with self.connection() as connection:
            run_result = await connection.execute(
                """SELECT r.*,m.role
                   FROM agent_runs r
                   LEFT JOIN workspace_memberships m
                     ON m.workspace_id=r.workspace_id
                    AND m.issuer=r.requested_by_issuer
                    AND m.subject=r.requested_by_subject
                   WHERE r.id=%s AND r.workspace_id=%s
                   FOR UPDATE OF r""",
                (context.run_id, context.workspace_id),
            )
            run = await run_result.fetchone()
            if (
                not run
                or run["agent_id"] != context.agent_id
                or run["trace_id"] != context.trace_id
                or run["status"] != "running"
                or run["cancel_requested_at"] is not None
                or run["lease_owner"] != context.worker_id
                or run["lease_expires_at"] is None
            ):
                raise ToolGatewayError("run_not_executable")
            now_result = await connection.execute("SELECT now() AS now")
            now = (await now_result.fetchone())["now"]
            if run["lease_expires_at"] <= now:
                raise ToolGatewayError("run_lease_expired")
            receipt_result = await connection.execute(
                """SELECT status,worker_id,lease_expires_at FROM worker_job_receipts
                   WHERE job_id=%s AND run_id=%s FOR UPDATE""",
                (context.job_id, context.run_id),
            )
            receipt = await receipt_result.fetchone()
            if (
                not receipt
                or receipt["status"] != "processing"
                or receipt["worker_id"] != context.worker_id
                or receipt["lease_expires_at"] is None
                or receipt["lease_expires_at"] <= now
            ):
                raise ToolGatewayError("run_not_executable")
            actor = Principal(run["requested_by_issuer"], run["requested_by_subject"])
            from nexora_api.workspaces import authorize

            authorize(run["role"], Permission.RUN_AGENTS)

            existing_result = await connection.execute(
                """SELECT * FROM tool_calls
                   WHERE workspace_id=%s AND run_id=%s
                     AND requested_by_issuer=%s AND requested_by_subject=%s
                     AND idempotency_key=%s FOR UPDATE""",
                (
                    context.workspace_id,
                    context.run_id,
                    actor.issuer,
                    actor.subject,
                    idempotency_key,
                ),
            )
            existing = await existing_result.fetchone()
            if existing:
                tool_call_id = existing["id"]
                if (
                    existing["tool_name"] != spec.name
                    or existing["arguments_hash"] != arguments_hash
                ):
                    raise ToolGatewayError("tool_idempotency_conflict")
                if existing["status"] == "succeeded":
                    return ToolCallOutcome(
                        tool_call_id, "succeeded", output=existing["result"]
                    )
                if existing["status"] in (
                    "failed",
                    "rejected",
                    "cancelled",
                    "expired",
                    "denied",
                ):
                    return ToolCallOutcome(
                        tool_call_id,
                        existing["status"],
                        error_code=existing["error_code"],
                    )
                if existing["status"] == "waiting_approval":
                    approval_result = await connection.execute(
                        """SELECT id FROM tool_approvals
                           WHERE tool_call_id=%s AND status='pending'""",
                        (tool_call_id,),
                    )
                    approval = await approval_result.fetchone()
                    if not approval:
                        raise ToolGatewayError("approval_state_invalid")
                    approval_id = approval["id"]
                elif existing["status"] in ("approved", "executing"):
                    approval_result = await connection.execute(
                        """SELECT status,arguments_hash FROM tool_approvals
                           WHERE tool_call_id=%s
                           ORDER BY requested_at DESC LIMIT 1""",
                        (tool_call_id,),
                    )
                    prior_approval = await approval_result.fetchone()
                    approved_before = bool(
                        prior_approval
                        and prior_approval["status"] == "approved"
                        and prior_approval["arguments_hash"] == arguments_hash
                    )
                    if existing["status"] == "approved" and not approved_before:
                        raise ToolGatewayError("approval_state_invalid")
                else:
                    raise ToolGatewayError("tool_state_invalid")

            policy = await evaluate_policy(connection, context.workspace_id, spec)
            if existing is None:
                tool_call_id = uuid4()
                if policy.decision == PolicyDecision.DENY:
                    await connection.execute(
                        """INSERT INTO tool_calls
                           (id,workspace_id,run_id,tool_name,schema_version,side_effect,
                            arguments,arguments_hash,idempotency_key,requested_by_issuer,
                            requested_by_subject,policy_decision,policy_reason,status,error_code,
                            finished_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'denied',
                                   'policy_denied',now())""",
                        (
                            tool_call_id,
                            context.workspace_id,
                            context.run_id,
                            spec.name,
                            spec.schema_version,
                            spec.side_effect,
                            Jsonb(normalized),
                            arguments_hash,
                            idempotency_key,
                            actor.issuer,
                            actor.subject,
                            policy.decision,
                            policy.reason,
                        ),
                    )
                    await append_run_event(
                        connection,
                        context.workspace_id,
                        context.run_id,
                        "tool.denied",
                        {
                            "tool_call_id": str(tool_call_id),
                            "tool_name": spec.name,
                            "arguments_hash": arguments_hash,
                            "reason": policy.reason,
                        },
                    )
                    return ToolCallOutcome(tool_call_id, "denied", error_code="policy_denied")

                if policy.decision == PolicyDecision.REQUIRE_APPROVAL:
                    approval_id = uuid4()
                    await connection.execute(
                        """INSERT INTO tool_calls
                           (id,workspace_id,run_id,tool_name,schema_version,side_effect,
                            arguments,arguments_hash,idempotency_key,requested_by_issuer,
                            requested_by_subject,policy_decision,policy_reason,status)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'waiting_approval')""",
                        (
                            tool_call_id,
                            context.workspace_id,
                            context.run_id,
                            spec.name,
                            spec.schema_version,
                            spec.side_effect,
                            Jsonb(normalized),
                            arguments_hash,
                            idempotency_key,
                            actor.issuer,
                            actor.subject,
                            policy.decision,
                            policy.reason,
                        ),
                    )
                    await connection.execute(
                        """INSERT INTO tool_approvals
                           (id,workspace_id,run_id,tool_call_id,arguments_hash,
                            requested_by_issuer,requested_by_subject,status,policy_reason,
                            expires_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,
                                   now()+(%s * interval '1 second'))""",
                        (
                            approval_id,
                            context.workspace_id,
                            context.run_id,
                            tool_call_id,
                            arguments_hash,
                            actor.issuer,
                            actor.subject,
                            policy.reason,
                            self.settings.tool_approval_ttl_seconds,
                        ),
                    )
                    await connection.execute(
                        """UPDATE agent_runs
                           SET status='waiting_for_approval',lease_owner=NULL,
                               lease_expires_at=NULL,updated_at=now()
                           WHERE id=%s""",
                        (context.run_id,),
                    )
                    await connection.execute(
                        """UPDATE worker_job_receipts
                           SET status='superseded',lease_expires_at=NULL,
                               last_error_code='waiting_for_approval',updated_at=now()
                           WHERE job_id=%s AND run_id=%s AND worker_id=%s
                             AND status='processing'""",
                        (context.job_id, context.run_id, context.worker_id),
                    )
                    await append_run_event(
                        connection,
                        context.workspace_id,
                        context.run_id,
                        "tool.approval_requested",
                        {
                            "tool_call_id": str(tool_call_id),
                            "approval_id": str(approval_id),
                            "tool_name": spec.name,
                            "arguments_hash": arguments_hash,
                            "reason": policy.reason,
                        },
                    )
                else:
                    await connection.execute(
                        """INSERT INTO tool_calls
                           (id,workspace_id,run_id,tool_name,schema_version,side_effect,
                            arguments,arguments_hash,idempotency_key,requested_by_issuer,
                            requested_by_subject,policy_decision,policy_reason,status,started_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'executing',now())""",
                        (
                            tool_call_id,
                            context.workspace_id,
                            context.run_id,
                            spec.name,
                            spec.schema_version,
                            spec.side_effect,
                            Jsonb(normalized),
                            arguments_hash,
                            idempotency_key,
                            actor.issuer,
                            actor.subject,
                            policy.decision,
                            policy.reason,
                        ),
                    )
            elif approval_id is None:
                if policy.decision == PolicyDecision.DENY:
                    await connection.execute(
                        """UPDATE tool_calls
                           SET status='denied',policy_decision='deny',
                               policy_reason=%s,error_code='policy_denied',
                               finished_at=now(),updated_at=now()
                           WHERE id=%s""",
                        (policy.reason, tool_call_id),
                    )
                    await append_run_event(
                        connection,
                        context.workspace_id,
                        context.run_id,
                        "tool.denied",
                        {
                            "tool_call_id": str(tool_call_id),
                            "tool_name": spec.name,
                            "arguments_hash": arguments_hash,
                            "reason": policy.reason,
                        },
                    )
                    return ToolCallOutcome(tool_call_id, "denied", error_code="policy_denied")
                if (
                    policy.decision == PolicyDecision.REQUIRE_APPROVAL
                    and existing["status"] == "executing"
                    and not approved_before
                ):
                    approval_id = uuid4()
                    await connection.execute(
                        """UPDATE tool_calls
                           SET status='waiting_approval',policy_decision=%s,
                               policy_reason=%s,updated_at=now()
                           WHERE id=%s""",
                        (policy.decision, policy.reason, tool_call_id),
                    )
                    await connection.execute(
                        """INSERT INTO tool_approvals
                           (id,workspace_id,run_id,tool_call_id,arguments_hash,
                            requested_by_issuer,requested_by_subject,status,policy_reason,
                            expires_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,
                                   now()+(%s * interval '1 second'))""",
                        (
                            approval_id,
                            context.workspace_id,
                            context.run_id,
                            tool_call_id,
                            arguments_hash,
                            actor.issuer,
                            actor.subject,
                            policy.reason,
                            self.settings.tool_approval_ttl_seconds,
                        ),
                    )
                    await connection.execute(
                        """UPDATE agent_runs
                           SET status='waiting_for_approval',lease_owner=NULL,
                               lease_expires_at=NULL,updated_at=now()
                           WHERE id=%s""",
                        (context.run_id,),
                    )
                    await connection.execute(
                        """UPDATE worker_job_receipts
                           SET status='superseded',lease_expires_at=NULL,
                               last_error_code='waiting_for_approval',updated_at=now()
                           WHERE job_id=%s AND run_id=%s AND worker_id=%s
                             AND status='processing'""",
                        (context.job_id, context.run_id, context.worker_id),
                    )
                    await append_run_event(
                        connection,
                        context.workspace_id,
                        context.run_id,
                        "tool.approval_requested",
                        {
                            "tool_call_id": str(tool_call_id),
                            "approval_id": str(approval_id),
                            "tool_name": spec.name,
                            "arguments_hash": arguments_hash,
                            "reason": policy.reason,
                        },
                    )
                else:
                    await connection.execute(
                        """UPDATE tool_calls
                           SET status='executing',policy_decision=%s,policy_reason=%s,
                               started_at=COALESCE(started_at,now()),updated_at=now(),
                               error_code=NULL
                           WHERE id=%s""",
                        (policy.decision, policy.reason, tool_call_id),
                    )

            if approval_id is None:
                await append_run_event(
                    connection,
                    context.workspace_id,
                    context.run_id,
                    "tool.execution_started",
                    {
                        "tool_call_id": str(tool_call_id),
                        "tool_name": spec.name,
                        "arguments_hash": arguments_hash,
                    },
                )

        if approval_id is not None:
            raise ToolApprovalRequired(tool_call_id, approval_id)

        execution_context = ToolExecutionContext(
            tool_call_id=tool_call_id,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            actor_issuer=actor.issuer,
            actor_subject=actor.subject,
            idempotency_key=idempotency_key,
        )
        started = time.monotonic()
        try:
            output_model = await self.registry.execute(spec, execution_context, validated)
            output, result_hash = canonical_payload(output_model)
        except ToolExecutionError as exc:
            return await self._finalize_failure(
                context, tool_call_id, exc.code, started
            )
        except Exception:
            return await self._finalize_failure(
                context, tool_call_id, "tool_execution_error", started
            )
        return await self._finalize_success(
            context, tool_call_id, output, result_hash, started
        )

    async def _finalize_success(
        self, context, tool_call_id, output, result_hash, started
    ):
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        async with self.connection() as connection:
            if not await self._worker_still_owns_run(connection, context):
                raise ToolGatewayError("run_lease_lost")
            call_result = await connection.execute(
                "SELECT status FROM tool_calls WHERE id=%s FOR UPDATE", (tool_call_id,)
            )
            call = await call_result.fetchone()
            if not call or call["status"] != "executing":
                raise ToolGatewayError("tool_state_invalid")
            await connection.execute(
                """UPDATE tool_calls
                   SET status='succeeded',result=%s,result_hash=%s,error_code=NULL,
                       finished_at=now(),duration_ms=%s,updated_at=now()
                   WHERE id=%s""",
                (Jsonb(output), result_hash, duration_ms, tool_call_id),
            )
            await append_run_event(
                connection,
                context.workspace_id,
                context.run_id,
                "tool.succeeded",
                {"tool_call_id": str(tool_call_id), "result_hash": result_hash},
            )
        return ToolCallOutcome(tool_call_id, "succeeded", output=output)

    async def _finalize_failure(
        self, context, tool_call_id, error_code, started
    ):
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        async with self.connection() as connection:
            if not await self._worker_still_owns_run(connection, context):
                raise ToolGatewayError("run_lease_lost")
            call_result = await connection.execute(
                "SELECT status FROM tool_calls WHERE id=%s FOR UPDATE", (tool_call_id,)
            )
            call = await call_result.fetchone()
            if not call or call["status"] != "executing":
                raise ToolGatewayError("tool_state_invalid")
            await connection.execute(
                """UPDATE tool_calls
                   SET status='failed',error_code=%s,finished_at=now(),
                       duration_ms=%s,updated_at=now()
                   WHERE id=%s""",
                (error_code[:100], duration_ms, tool_call_id),
            )
            await append_run_event(
                connection,
                context.workspace_id,
                context.run_id,
                "tool.failed",
                {"tool_call_id": str(tool_call_id), "error_code": error_code[:100]},
            )
        return ToolCallOutcome(tool_call_id, "failed", error_code=error_code[:100])

    async def _worker_still_owns_run(self, connection, context):
        result = await connection.execute(
            """SELECT 1 FROM agent_runs
               WHERE id=%s AND workspace_id=%s AND status='running'
                 AND lease_owner=%s AND lease_expires_at > now()""",
            (context.run_id, context.workspace_id, context.worker_id),
        )
        return await result.fetchone() is not None
