import { z } from "zod";

const count = z.number().int().nonnegative();
const timestamp = z.iso.datetime({ offset: true });

export const runStatusSchema = z.enum([
  "queued", "running", "waiting_for_approval", "succeeded", "failed", "cancelled",
]);
export type RunStatus = z.infer<typeof runStatusSchema>;
export const TERMINAL_STATUSES: RunStatus[] = ["succeeded", "failed", "cancelled"];

export const agentSchema = z.object({
  id: z.uuid(), workspace_id: z.uuid(), name: z.string().min(1).max(100),
  instructions: z.string().min(1).max(20000),
  model_profile: z.string().min(1).max(64), created_at: timestamp,
});
export const agentPageSchema = z.object({
  items: z.array(agentSchema), next_cursor: z.uuid().nullable(),
});
export const agentInput = z.object({
  name: z.string().trim().min(1).max(100),
  instructions: z.string().trim().min(1).max(20000),
  // Matches the API pattern: a profile is operator configuration, not free text.
  model_profile: z.string().trim().min(1).max(64).regex(/^[A-Za-z0-9._-]+$/),
});

export const runInput = z.object({
  agent_id: z.uuid(), input: z.string().trim().min(1).max(20000),
});

const runShape = {
  id: z.uuid(), workspace_id: z.uuid(), agent_id: z.uuid(), trace_id: z.uuid(),
  status: runStatusSchema, attempt_count: count,
  cancel_requested_at: timestamp.nullable(), finished_at: timestamp.nullable(),
  failure_code: z.string().max(200).nullable(),
  created_at: timestamp, updated_at: timestamp,
};
const terminalIsFinished = (run: { status: RunStatus; finished_at: string | null }) =>
  TERMINAL_STATUSES.includes(run.status) === (run.finished_at !== null);

export const runSchema = z.object(runShape).refine(
  terminalIsFinished, "A run is finished exactly when it reaches a terminal status",
);
export const runSummarySchema = z.object({
  ...runShape, agent_name: z.string().min(1).max(100), requested_by_me: z.boolean(),
}).refine(terminalIsFinished, "A run is finished exactly when it reaches a terminal status");
export const runPageSchema = z.object({
  items: z.array(runSummarySchema), next_cursor: z.uuid().nullable(),
});

export const runEventSchema = z.object({
  id: z.uuid(), event_no: z.number().int().positive(), event_type: z.string().min(1).max(100),
  payload: z.record(z.string(), z.unknown()), created_at: timestamp,
});
export const runEventPageSchema = z.object({
  items: z.array(runEventSchema), next_cursor: z.number().int().positive().nullable(),
});

export const runResultSchema = z.object({
  run_id: z.uuid(), workspace_id: z.uuid(), agent_id: z.uuid(), trace_id: z.uuid(),
  status: z.enum(["succeeded", "failed", "cancelled"]),
  output_text: z.string().nullable(),
  finish_reason: z.enum(["stop", "refusal"]).nullable(),
  failure_code: z.string().max(200).nullable(),
  recorded_input_tokens: count, recorded_output_tokens: count,
  selected_tools: z.array(z.string()),
  model_steps: z.array(z.object({
    step_no: count, provider: z.string().min(1).max(64), model: z.string().min(1).max(128),
    finish_reason: z.string().min(1).max(64), input_tokens: count, output_tokens: count,
  })),
}).refine(
  // A non-successful run never carries an answer; the API states that and the console
  // refuses to render a payload that contradicts it.
  result => result.status === "succeeded" || (result.output_text === null && result.finish_reason === null),
  "Invalid run result",
);

export type Agent = z.infer<typeof agentSchema>;
export type AgentRun = z.infer<typeof runSchema>;
export type AgentRunSummary = z.infer<typeof runSummarySchema>;
export type RunEvent = z.infer<typeof runEventSchema>;
export type RunResult = z.infer<typeof runResultSchema>;

export const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  queued: "Queued",
  running: "Running",
  waiting_for_approval: "Waiting for approval",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
};

// Colour never carries the message alone: every status is written out next to it.
export const RUN_STATUS_TONES: Record<RunStatus, string> = {
  queued: "pending",
  running: "unknown",
  waiting_for_approval: "unknown",
  succeeded: "up",
  failed: "down",
  cancelled: "pending",
};

export const RUN_STATUS_HELP: Record<RunStatus, string> = {
  queued: "Waiting for a worker. Nothing runs unless an agent worker is configured and started.",
  running: "A worker holds a lease on this run and is executing it.",
  waiting_for_approval: "A governed tool needs a human decision before this run can continue.",
  succeeded: "The run finished and recorded a final answer for the person who started it.",
  failed: "The run stopped with a recorded failure code. No partial answer is published.",
  cancelled: "The run was cancelled. No partial answer is published.",
};

// Run events are the append-only execution history. Types the console does not know
// are still shown, so a new worker event never disappears from the timeline.
export const RUN_EVENT_LABELS: Record<string, string> = {
  "run.queued": "Queued",
  "run.started": "Worker started the run",
  "run.resumed": "Resumed after approval",
  "run.retry_scheduled": "Retry scheduled",
  "run.redis_requeued": "Requeued for delivery",
  "run.recovered": "Recovered after a lost lease",
  "run.waiting_for_approval": "Paused for approval",
  "run.cancel_requested": "Cancellation requested",
  "run.cancelled": "Cancelled",
  "run.succeeded": "Succeeded",
  "run.failed": "Failed",
  "model.completed": "Model step recorded",
  "retrieval.completed": "Retrieval completed",
  "tool.call_planned": "Tool call planned",
  "tool.approval_requested": "Approval requested",
  "tool.approved": "Tool approved",
  "tool.denied": "Tool denied by policy",
  "tool.rejected": "Tool rejected by an approver",
  "tool.started": "Tool started",
  "tool.succeeded": "Tool succeeded",
  "tool.failed": "Tool failed",
  "tool.retry_scheduled": "Tool retry scheduled",
  "tool.contract_changed": "Tool contract changed",
  "spend.denied": "Stopped by the workspace budget",
};

export function runEventLabel(type: string): string {
  return RUN_EVENT_LABELS[type] ?? type;
}

export function runTimestamp(value: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium", timeStyle: "short", timeZone: "UTC",
  }).format(new Date(value)) + " UTC";
}

export function runDuration(run: { created_at: string; finished_at: string | null }): string | null {
  if (!run.finished_at) return null;
  const seconds = Math.max(0, Math.round((Date.parse(run.finished_at) - Date.parse(run.created_at)) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

// Payload values are worker-written identifiers, counts and codes. They are rendered as
// text only: the timeline never interprets one as markup or as a link.
export function eventDetail(payload: Record<string, unknown>): string {
  const parts = Object.entries(payload)
    .filter(([key]) => key !== "request_id")
    .map(([key, value]) => {
      const text = value === null || value === undefined
        ? "—"
        : typeof value === "object" ? JSON.stringify(value) : String(value);
      return `${key}: ${text.length > 120 ? text.slice(0, 117) + "…" : text}`;
    });
  return parts.join(" · ");
}
