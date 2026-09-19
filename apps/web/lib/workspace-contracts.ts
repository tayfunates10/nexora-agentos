import { z } from "zod";
export const workspaceSchema = z.object({ id: z.uuid(), name: z.string(), role: z.enum(["owner", "admin", "member"]) });
export const pageSchema = z.object({ items: z.array(workspaceSchema), next_cursor: z.uuid().nullable() });
export const workspaceInput = z.object({ name: z.string().trim().min(1).max(100) });
export const memberInput = z.object({ subject: z.string().trim().min(1).max(255), role: z.enum(["admin", "member"]) });
export type Workspace = z.infer<typeof workspaceSchema>;
