import { z } from "zod";
import type { Tone } from "./i18n/tone.ts";
import type { MessageKey } from "../messages/en.ts";

const count = z.number().int().nonnegative();
const timestamp = z.iso.datetime({ offset: true });

export const accessScopeSchema = z.enum(["workspace", "restricted"]);
export const ingestionStatusSchema = z.enum([
  "queued", "running", "succeeded", "failed", "cancelled",
]);
export type AccessScope = z.infer<typeof accessScopeSchema>;
export type IngestionStatus = z.infer<typeof ingestionStatusSchema>;

export const SOURCE_KEY = /^[A-Za-z0-9._:-]+$/;
export const MAX_SOURCE_CHARS = 500_000;

export const knowledgeSourceSchema = z.object({
  id: z.uuid(), source_key: z.string().min(1).max(255), version: z.string().min(1).max(128),
  title: z.string().min(1).max(500), access_scope: accessScopeSchema,
  content_hash: z.string().length(64), chunk_count: count,
  created_at: timestamp, updated_at: timestamp,
});
export const knowledgeSourcePageSchema = z.object({
  items: z.array(knowledgeSourceSchema), next_cursor: z.uuid().nullable(),
});

export const ingestionSchema = z.object({
  id: z.uuid(), workspace_id: z.uuid(), source_key: z.string().min(1).max(255),
  version: z.string().min(1).max(128), title: z.string().min(1).max(500),
  access_scope: accessScopeSchema, status: ingestionStatusSchema, attempt_count: count,
  source_id: z.uuid().nullable(), chunk_count: count.nullable(),
  embedding_input_tokens: count.nullable(), error_code: z.string().max(200).nullable(),
  created_at: timestamp, updated_at: timestamp, finished_at: timestamp.nullable(),
}).refine(
  // Indexed content only exists once a job has succeeded, and a failure names its code.
  job => (job.status === "succeeded") === (job.source_id !== null)
    && (job.status !== "failed" || job.error_code !== null),
  "Invalid ingestion state",
);

export const sourceInput = z.object({
  source_key: z.string().trim().min(1).max(255).regex(SOURCE_KEY),
  version: z.string().trim().min(1).max(128),
  title: z.string().trim().min(1).max(500),
  text: z.string().trim().min(1).max(MAX_SOURCE_CHARS),
  access_scope: accessScopeSchema,
});

export type KnowledgeSource = z.infer<typeof knowledgeSourceSchema>;
export type Ingestion = z.infer<typeof ingestionSchema>;

export function scopeKey(scope: AccessScope): MessageKey {
  return `knowledge.scope.${scope}`;
}

export function ingestionStatusKey(status: IngestionStatus): MessageKey {
  return `knowledge.ingestion.${status}`;
}

export function ingestionHelpKey(status: IngestionStatus): MessageKey {
  return `knowledge.ingestionHelp.${status}`;
}

export const INGESTION_STATUS_TONES: Record<IngestionStatus, Tone> = {
  queued: "neutral",
  running: "warn",
  succeeded: "up",
  failed: "down",
  cancelled: "neutral",
};
