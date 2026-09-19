from dataclasses import dataclass

from nexora_api.tool_registry import PolicyDecision, SideEffect, ToolSpec


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str


def default_policy(spec: ToolSpec) -> PolicyResult:
    if spec.side_effect == SideEffect.READ:
        return PolicyResult(PolicyDecision.ALLOW, "default_read_only")
    return PolicyResult(PolicyDecision.REQUIRE_APPROVAL, "default_mutation_approval")


async def evaluate_policy(connection, workspace_id, spec: ToolSpec) -> PolicyResult:
    result = await connection.execute(
        """SELECT decision FROM workspace_tool_policies
           WHERE workspace_id=%s AND tool_name=%s""",
        (workspace_id, spec.name),
    )
    row = await result.fetchone()
    if not row:
        return default_policy(spec)
    return PolicyResult(PolicyDecision(row["decision"]), "workspace_policy")
