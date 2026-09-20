from fastapi import HTTPException

from nexora_api.run_state import ExecutionContext
from nexora_api.tool_contracts import ToolContractError
from nexora_api.workspaces import Permission, authorize


async def executable_run(connection, context: ExecutionContext):
    """Lock the run before external dispatch or durable executor/tool writes."""
    result = await connection.execute(
        """SELECT r.*, (r.lease_expires_at > clock_timestamp()) AS live_lease
           FROM agent_runs r WHERE r.id=%s AND r.workspace_id=%s FOR UPDATE""",
        (context.run_id, context.workspace_id),
    )
    run = await result.fetchone()
    if (
        not run
        or run["status"] != "running"
        or not run["live_lease"]
        or run["cancel_requested_at"] is not None
        or run["attempt_count"] != context.attempt_count
        or run["agent_id"] != context.agent_id
        or run["trace_id"] != context.trace_id
    ):
        raise ToolContractError("execution_fenced")
    receipt = await connection.execute(
        """SELECT 1 FROM worker_job_receipts
           WHERE job_id=%s AND workspace_id=%s AND run_id=%s
             AND status='processing' AND worker_id=%s
             AND lease_expires_at > clock_timestamp()""",
        (context.job_id, context.workspace_id, context.run_id, run["lease_owner"]),
    )
    if not await receipt.fetchone():
        raise ToolContractError("execution_fenced")
    member = await connection.execute(
        """SELECT role FROM workspace_memberships
           WHERE workspace_id=%s AND issuer=%s AND subject=%s FOR SHARE""",
        (context.workspace_id, run["requested_by_issuer"], run["requested_by_subject"]),
    )
    membership = await member.fetchone()
    try:
        authorize(membership["role"] if membership else None, Permission.RUN_AGENTS)
    except HTTPException as exc:
        raise ToolContractError("requester_not_authorized") from exc
    return run
