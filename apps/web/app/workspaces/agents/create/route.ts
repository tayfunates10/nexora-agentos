import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { agentInput, agentSchema } from "../../../../lib/agent-contracts";
import { readConfig, validMutation } from "../../../../lib/auth-core";
import { api, ApiError } from "../../../../lib/server/api";
import { readForm, TEXT_FORM_BYTES } from "../../../../lib/server/forms";
import { currentSession } from "../../../../lib/server/session";

export async function POST(request: NextRequest) {
  let config;
  try { config = readConfig(); } catch { return new NextResponse("Unavailable", { status: 503 }); }
  if (!config) return new NextResponse("Unavailable", { status: 503 });

  let target = "/workspaces";
  try {
    const session = await currentSession();
    if (!session) return NextResponse.redirect(new URL("/login", config.origin), 303);
    const form = await readForm(request, TEXT_FORM_BYTES);
    if (!validMutation(
      request.headers.get("origin"), config, form.get("csrf") ?? "", session.csrf,
      request.headers.get("referer"),
    )) return new NextResponse("Forbidden", { status: 403 });

    const workspaceId = z.uuid().parse(form.get("workspace"));
    target = `/workspaces/${workspaceId}/agents`;
    const parsed = agentInput.safeParse({
      name: form.get("name"),
      instructions: form.get("instructions"),
      model_profile: form.get("model_profile"),
    });
    if (!parsed.success) {
      return NextResponse.redirect(new URL(target + "?error=invalid", config.origin), 303);
    }

    await api(session, `/api/v1/workspaces/${workspaceId}/agents`, agentSchema, {
      method: "POST", body: JSON.stringify(parsed.data),
    });
    return NextResponse.redirect(new URL(target + "?saved=agent", config.origin), 303);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return NextResponse.redirect(new URL("/login?error=session_expired", config.origin), 303);
    }
    const reason = error instanceof ApiError && error.status === 403 ? "forbidden" : "failed";
    return NextResponse.redirect(new URL(target + "?error=" + reason, config.origin), 303);
  }
}
