import { z } from "zod";

export const healthSchema = z.object({
  status: z.enum(["ok", "degraded"]),
  service: z.literal("nexora-api"),
  version: z.string(),
  dependencies: z.object({
    postgres: z.enum(["up", "down"]),
    redis: z.enum(["up", "down"]),
  }),
});
export type Health = z.infer<typeof healthSchema>;
export type ApiState =
  | { kind: "connected"; health: Health }
  | { kind: "unavailable" }
  | { kind: "forbidden" }
  | { kind: "invalid" };

export async function fetchHealth(baseUrl: string, request: typeof fetch = fetch): Promise<ApiState> {
  try {
    const response = await request(new URL("/api/v1/health/ready", baseUrl), {
      cache: "no-store", signal: AbortSignal.timeout(5000),
    });
    if (response.status === 401 || response.status === 403) return { kind: "forbidden" };
    if (response.status !== 200 && response.status !== 503) return { kind: "unavailable" };
    const result = healthSchema.safeParse(await response.json());
    if (!result.success) return { kind: "invalid" };
    const allUp = result.data.dependencies.postgres === "up" && result.data.dependencies.redis === "up";
    if ((result.data.status === "ok") !== allUp || (response.status === 200) !== allUp) {
      return { kind: "invalid" };
    }
    return { kind: "connected", health: result.data };
  } catch {
    return { kind: "unavailable" };
  }
}
