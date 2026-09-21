import { z } from "zod";

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

export const SCOPE_LABELS: Record<AccessScope, string> = {
  workspace: "Everyone in this workspace",
  restricted: "Restricted to named accounts",
};

export const INGESTION_STATUS_LABELS: Record<IngestionStatus, string> = {
  queued: "Queued for the worker",
  running: "Embedding and indexing",
  succeeded: "Indexed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export const INGESTION_STATUS_TONES: Record<IngestionStatus, string> = {
  queued: "pending",
  running: "unknown",
  succeeded: "up",
  failed: "down",
  cancelled: "pending",
};

export const INGESTION_STATUS_HELP: Record<IngestionStatus, string> = {
  queued: "Stored in PostgreSQL. A retrieval-enabled worker embeds it; nothing is sent to a provider until then.",
  running: "A worker is embedding this version. Its cost is metered against the workspace budget.",
  succeeded: "Indexed and retrievable, subject to this source's access scope.",
  failed: "Nothing was indexed for this version. The recorded code says why.",
  cancelled: "The job was cancelled, usually because the source was deleted before it ran.",
};

export function knowledgeTimestamp(value: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium", timeStyle: "short", timeZone: "UTC",
  }).format(new Date(value)) + " UTC";
}
