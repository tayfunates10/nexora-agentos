import "server-only";
import type { Session } from "../auth-core";
import { z } from "zod";
export class ApiError extends Error { constructor(public status: number) { super("API request failed"); } }
export async function api<T>(session: Session, path: string, schema: z.ZodType<T>, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  headers.set("Authorization", "Bearer " + session.accessToken);
  const response = await fetch(new URL(path, process.env.NEXORA_API_URL ?? "http://127.0.0.1:8000"), {
    ...init, headers, cache: "no-store", redirect: "error", signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) throw new ApiError(response.status);
  return schema.parse(await response.json());
}
