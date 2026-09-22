import { z } from "zod";
import type { Tone } from "./i18n/tone.ts";
import type { MessageKey } from "../messages/en.ts";

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

export const runActionStatusSchema = z.enum([
  "planned", "pending_approval", "approved", "running",
  "succeeded", "failed", "denied", "cancelled",
]);
export type RunActionStatus = z.infer<typeof runActionStatusSchema>;

export const runActionSchema = z.object({
  id: z.uuid(), workspace_id: z.uuid(), run_id: z.uuid(),
  action_name: z.string().min(1).max(128),
  side_effect: sideEffectSchema,
  status: runActionStatusSchema,
  policy_decision: policyDecisionSchema,
  attempt_count: z.number().int().nonnegative(),
  error_code: z.string().max(100).nullable(),
  approval_id: z.uuid().nullable(),
  approval_status: approvalStatusSchema.nullable(),
  created_at: timestamp, updated_at: timestamp,
  started_at: timestamp.nullable(), finished_at: timestamp.nullable(),
});
export const runActionPageSchema = z.object({
  items: z.array(runActionSchema), next_cursor: z.uuid().nullable(),
});
export type RunAction = z.infer<typeof runActionSchema>;


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

export function sideEffectKey(value: SideEffect): MessageKey {
  return `tools.sideEffect.${value}`;
}

// Two side effects always need a human decision, whatever the stored policy says.
export const ALWAYS_APPROVED_SIDE_EFFECTS: SideEffect[] = ["destructive", "external_communication"];

export function policyKey(decision: PolicyDecision): MessageKey {
  return `tools.policy.${decision}`;
}

export const POLICY_TONES: Record<PolicyDecision, Tone> = {
  allow: "up",
  deny: "down",
  require_approval: "warn",
};

export function runActionStatusKey(status: RunActionStatus): MessageKey {
  return `runDetail.actions.status.${status}`;
}

export function approvalStatusKey(status: ApprovalStatus): MessageKey {
  return `approvals.status.${status}`;
}

export const APPROVAL_STATUS_TONES: Record<ApprovalStatus, Tone> = {
  pending: "warn",
  approved: "up",
  rejected: "down",
  expired: "neutral",
  cancelled: "neutral",
};

/** A tool with no stored policy is denied; the label says so rather than staying blank. */
export function policyLabelKey(tool: Tool): MessageKey {
  return tool.policy_decision === null ? "tools.policy.none" : policyKey(tool.policy_decision);
}

export function policyTone(tool: Tool): Tone {
  return tool.policy_decision === null ? "down" : POLICY_TONES[tool.policy_decision];
}

// An allow on a destructive or external-communication tool never removes the human step,
// and the console says so where the policy is read and where it is set.
export function forcesApproval(tool: Tool): boolean {
  return tool.policy_decision === "allow"
    && ALWAYS_APPROVED_SIDE_EFFECTS.includes(tool.side_effect);
}

/**
 * How long a pending approval has left. An approval whose window has closed reports that
 * it expired rather than counting into negative time.
 */
export type RemainingTime =
  | { unit: "expired" }
  | { unit: "seconds" | "minutes" | "hours"; count: number };

export function remainingTime(
  approval: { expires_at: string }, now: number = Date.now(),
): RemainingTime {
  const seconds = Math.round((Date.parse(approval.expires_at) - now) / 1000);
  if (seconds <= 0) return { unit: "expired" };
  if (seconds < 60) return { unit: "seconds", count: seconds };
  const minutes = Math.floor(seconds / 60);
  return minutes < 60
    ? { unit: "minutes", count: minutes }
    : { unit: "hours", count: Math.floor(minutes / 60) };
}

// Arguments are model-proposed values. An approver has to see them exactly as they were
// normalized and stored, so they are pretty-printed as inert text, never interpreted and
// never translated.
export function formatArguments(value: Record<string, unknown>): string {
  return JSON.stringify(value, null, 2);
}
