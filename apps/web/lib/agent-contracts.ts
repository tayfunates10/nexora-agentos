import { z } from "zod";
import type { Tone } from "./i18n/tone.ts";
import type { MessageKey } from "../messages/en.ts";

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

// Colour never carries the message alone: every status is written out next to it, so
// these tones stay presentational and never move into a message dictionary.
export const RUN_STATUS_TONES: Record<RunStatus, Tone> = {
  queued: "neutral",
  running: "warn",
  waiting_for_approval: "warn",
  succeeded: "up",
  failed: "down",
  cancelled: "neutral",
};

export function runStatusKey(status: RunStatus): MessageKey {
  return `runs.status.${status}`;
}

export function runStatusHelpKey(status: RunStatus): MessageKey {
  return `runs.statusHelp.${status}`;
}

// Run events are the append-only execution history. A type the console does not know is
// still shown by its recorded identifier, so a new worker event never disappears from the
// timeline just because no one has named it yet.
export const KNOWN_RUN_EVENTS = [
  "run.queued", "run.started", "run.resumed", "run.retry_scheduled", "run.redis_requeued",
  "run.recovered", "run.waiting_for_approval", "run.cancel_requested", "run.cancelled",
  "run.succeeded", "run.failed", "model.completed", "retrieval.completed",
  "tool.call_planned", "tool.approval_requested", "tool.approved", "tool.denied",
  "tool.rejected", "tool.started", "tool.succeeded", "tool.failed",
  "tool.retry_scheduled", "tool.contract_changed",
  "task.created", "task.verified", "task.verification_failed", "spend.denied",
] as const;

export type KnownRunEvent = (typeof KNOWN_RUN_EVENTS)[number];

export function runEventKey(type: string): MessageKey | null {
  return (KNOWN_RUN_EVENTS as readonly string[]).includes(type)
    ? (`runEvent.${type}` as MessageKey)
    : null;
}

/** Elapsed seconds, or null while the run has not finished. Never negative. */
export function runDurationSeconds(run: { created_at: string; finished_at: string | null }): number | null {
  if (!run.finished_at) return null;
  return Math.max(0, Math.round((Date.parse(run.finished_at) - Date.parse(run.created_at)) / 1000));
}

// Payload values are worker-written identifiers, counts and codes. They are rendered as
// text only: the timeline never interprets one as markup or as a link, and the keys are
// the API's own field names, which are not translated.
export function eventDetail(payload: Record<string, unknown>): string {
  const parts = Object.entries(payload)
    .filter(([key]) => key !== "request_id")
    .map(([key, value]) => {
      const text = value === null || value === undefined
        ? "\u2014"
        : typeof value === "object" ? JSON.stringify(value) : String(value);
      return `${key}: ${text.length > 120 ? text.slice(0, 117) + "\u2026" : text}`;
    });
  return parts.join(" \u00b7 ");
}
