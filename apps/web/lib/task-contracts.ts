import { z } from "zod";
import type { MessageKey } from "../messages/en.ts";

const timestamp = z.iso.datetime({ offset: true });

export const runTaskStatusSchema = z.enum([
  "planned", "running", "succeeded", "failed", "cancelled", "blocked",
]);
export type RunTaskStatus = z.infer<typeof runTaskStatusSchema>;

export const verificationStateSchema = z.enum([
  "not_required", "pending", "verified", "failed",
]);
export type VerificationState = z.infer<typeof verificationStateSchema>;

export const runTaskEvidenceSchema = z.object({
  id: z.uuid(),
  verification_tool_name: z.string().min(1).max(128),
  summary: z.string().min(1).max(2000),
  satisfied: z.boolean(),
  created_at: timestamp,
});

export const runTaskSchema = z.object({
  id: z.uuid(),
  workspace_id: z.uuid(),
  run_id: z.uuid(),
  parent_task_id: z.uuid().nullable(),
  kind: z.enum(["goal", "follow_up"]),
  title: z.string().min(1).max(300),
  description: z.string().max(2000),
  status: runTaskStatusSchema,
  action_tool_name: z.string().min(2).max(128).nullable(),
  verification_state: verificationStateSchema,
  dependencies: z.array(z.uuid()),
  evidence: z.array(runTaskEvidenceSchema),
  created_at: timestamp,
  updated_at: timestamp,
  completed_at: timestamp.nullable(),
});

export const runTaskPageSchema = z.object({
  items: z.array(runTaskSchema),
  next_cursor: z.uuid().nullable(),
});

export type RunTask = z.infer<typeof runTaskSchema>;

export function runTaskStatusKey(status: RunTaskStatus): MessageKey {
  return `runDetail.plan.status.${status}`;
}

export function verificationStateKey(state: VerificationState): MessageKey {
  return `runDetail.plan.verification.${state}`;
}
