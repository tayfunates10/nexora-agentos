import "server-only";
import type { Session } from "../auth-core";
import { z } from "zod";
export class ApiError extends Error { constructor(public status: number) { super("API request failed"); } }

// One place holds the session access token: no route handler or page builds its own request.
async function request(session: Session, path: string, init: RequestInit): Promise<Response> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  headers.set("Authorization", "Bearer " + session.accessToken);
  const response = await fetch(new URL(path, process.env.NEXORA_API_URL ?? "http://127.0.0.1:8000"), {
    ...init, headers, cache: "no-store", redirect: "error", signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) throw new ApiError(response.status);
  return response;
}

export async function api<T>(session: Session, path: string, schema: z.ZodType<T>, init: RequestInit = {}): Promise<T> {
  return schema.parse(await (await request(session, path, init)).json());
}

// For endpoints that answer 204 with no body, such as deletion.
export async function apiNoContent(session: Session, path: string, init: RequestInit = {}): Promise<void> {
  await request(session, path, init);
}
