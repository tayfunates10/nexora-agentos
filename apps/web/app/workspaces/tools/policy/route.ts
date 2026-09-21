import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { readConfig, validMutation } from "../../../../lib/auth-core";
import { api, ApiError } from "../../../../lib/server/api";
import { readForm } from "../../../../lib/server/forms";
import { currentSession } from "../../../../lib/server/session";
import { policyInput } from "../../../../lib/tool-contracts";

const policySchema = z.object({
  workspace_id: z.uuid(), tool_id: z.uuid(),
  decision: z.enum(["allow", "deny", "require_approval"]),
  reason: z.string().min(1).max(500), updated_at: z.iso.datetime({ offset: true }),
});

export async function POST(request: NextRequest) {
  let config;
  try { config = readConfig(); } catch { return new NextResponse("Unavailable", { status: 503 }); }
  if (!config) return new NextResponse("Unavailable", { status: 503 });

  let target = "/workspaces";
  try {
    const session = await currentSession();
    if (!session) return NextResponse.redirect(new URL("/login", config.origin), 303);
    const form = await readForm(request);
    if (!validMutation(
      request.headers.get("origin"), config, form.get("csrf") ?? "", session.csrf,
      request.headers.get("referer"),
    )) return new NextResponse("Forbidden", { status: 403 });

    const workspaceId = z.uuid().parse(form.get("workspace"));
    target = `/workspaces/${workspaceId}/tools`;
    const tool = z.string().regex(/^[a-z][a-z0-9_.-]{1,63}$/).safeParse(form.get("tool"));
    const parsed = policyInput.safeParse({
      decision: form.get("decision"), reason: form.get("reason"),
    });
    if (!tool.success || !parsed.success) {
      return NextResponse.redirect(new URL(target + "?error=invalid", config.origin), 303);
    }

    await api(session, `/api/v1/workspaces/${workspaceId}/tools/${tool.data}/policy`,
      policySchema, { method: "PUT", body: JSON.stringify(parsed.data) });
    return NextResponse.redirect(new URL(target + "?saved=policy", config.origin), 303);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return NextResponse.redirect(new URL("/login?error=session_expired", config.origin), 303);
    }
    const reason = error instanceof ApiError && error.status === 403 ? "forbidden" : "failed";
    return NextResponse.redirect(new URL(target + "?error=" + reason, config.origin), 303);
  }
}
