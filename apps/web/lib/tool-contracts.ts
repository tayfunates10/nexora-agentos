import { z } from "zod";

const timestamp = z.iso.datetime({ offset: true });

export const sideEffectSchema = z.enum([
  "read", "write", "destructive", "external_communication",
]);
export const policyDecisionSchema = z.enum(["allow", "deny", "require_approval"]);
export const approvalStatusSchema = z.enum([
  "pending", "approved", "rejected", "expired", "cancelled",
]);
export type SideEffect = z.infer<typeof sideEffectSchema>;
export type PolicyDecision = z.infer<typeof policyDecisionSchema>;
export type ApprovalStatus = z.infer<typeof approvalStatusSchema>;

export const toolSchema = z.object({
  id: z.uuid(), workspace_id: z.uuid(), name: z.string().min(1).max(64),
  server_key: z.string().min(2).max(64), remote_name: z.string().min(1).max(128),
  description: z.string().min(1).max(1000),
  input_schema: z.record(z.string(), z.unknown()),
  output_schema: z.record(z.string(), z.unknown()).nullable(),
  side_effect: sideEffectSchema, enabled: z.boolean(),
  policy_decision: policyDecisionSchema.nullable(),
  policy_reason: z.string().max(500).nullable(),
  policy_updated_at: timestamp.nullable(),
  created_at: timestamp, updated_at: timestamp,
}).refine(
  // A stored policy always has a decision, the reason it was set and when. All three are
  // present together or none of them is.
  tool => (tool.policy_decision === null) === (tool.policy_reason === null)
    && (tool.policy_decision === null) === (tool.policy_updated_at === null),
  "Invalid tool policy",
);
export const toolPageSchema = z.object({
  items: z.array(toolSchema), next_cursor: z.string().max(64).nullable(),
});
export const policyInput = z.object({
  decision: policyDecisionSchema, reason: z.string().trim().min(1).max(500),
});

export const approvalSchema = z.object({
  id: z.uuid(), workspace_id: z.uuid(), run_id: z.uuid(), tool_call_id: z.uuid(),
  requested_action: z.string().min(1).max(128),
  normalized_arguments: z.record(z.string(), z.unknown()),
  requester_subject: z.string().min(1).max(255),
  approver_subject: z.string().max(255).nullable(),
  status: approvalStatusSchema,
  policy_reason: z.string().min(1).max(500),
  expires_at: timestamp, created_at: timestamp,
  decided_at: timestamp.nullable(),
}).refine(
  // Only a pending approval is undecided; every closed one records when it closed.
  approval => (approval.status === "pending") === (approval.decided_at === null),
  "Invalid approval state",
);
export const approvalPageSchema = z.object({
  items: z.array(approvalSchema), next_cursor: z.uuid().nullable(),
});
export const decisionInput = z.object({ decision: z.enum(["approved", "rejected"]) });

export type Tool = z.infer<typeof toolSchema>;
export type ToolApproval = z.infer<typeof approvalSchema>;

export const SIDE_EFFECT_LABELS: Record<SideEffect, string> = {
  read: "Read only",
  write: "Writes data",
  destructive: "Destructive",
  external_communication: "Sends external communication",
};

// Two side effects always need a human decision, whatever the stored policy says.
export const ALWAYS_APPROVED_SIDE_EFFECTS: SideEffect[] = ["destructive", "external_communication"];

export const POLICY_LABELS: Record<PolicyDecision, string> = {
  allow: "Allow",
  deny: "Deny",
  require_approval: "Require approval",
};

export const POLICY_TONES: Record<PolicyDecision, string> = {
  allow: "up",
  deny: "down",
  require_approval: "unknown",
};

export const APPROVAL_STATUS_LABELS: Record<ApprovalStatus, string> = {
  pending: "Waiting for a decision",
  approved: "Approved",
  rejected: "Rejected",
  expired: "Expired without a decision",
  cancelled: "Cancelled with its run",
};

export const APPROVAL_STATUS_TONES: Record<ApprovalStatus, string> = {
  pending: "unknown",
  approved: "up",
  rejected: "down",
  expired: "pending",
  cancelled: "pending",
};

export function policyLabel(tool: Tool): string {
  return tool.policy_decision === null
    ? "No policy · denied by default"
    : POLICY_LABELS[tool.policy_decision];
}

export function policyTone(tool: Tool): string {
  return tool.policy_decision === null ? "down" : POLICY_TONES[tool.policy_decision];
}

// An allow on a destructive or external-communication tool never removes the human step,
// and the console says so where the policy is read and where it is set.
export function forcesApproval(tool: Tool): boolean {
  return tool.policy_decision === "allow"
    && ALWAYS_APPROVED_SIDE_EFFECTS.includes(tool.side_effect);
}

export function toolTimestamp(value: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium", timeStyle: "short", timeZone: "UTC",
  }).format(new Date(value)) + " UTC";
}

export function expiresIn(approval: { expires_at: string }, now: number = Date.now()): string {
  const seconds = Math.round((Date.parse(approval.expires_at) - now) / 1000);
  if (seconds <= 0) return "expired";
  if (seconds < 60) return `${seconds}s left`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}m left` : `${Math.floor(minutes / 60)}h left`;
}

// Arguments are model-proposed values. An approver has to see them exactly as they were
// normalized and stored, so they are pretty-printed as inert text, never interpreted.
export function formatArguments(value: Record<string, unknown>): string {
  return JSON.stringify(value, null, 2);
}
