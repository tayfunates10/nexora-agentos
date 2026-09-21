import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { runSchema } from "../../../../lib/agent-contracts";
import { readConfig, validMutation } from "../../../../lib/auth-core";
import { api, ApiError } from "../../../../lib/server/api";
import { readForm } from "../../../../lib/server/forms";
import { currentSession } from "../../../../lib/server/session";

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
    const runId = z.uuid().parse(form.get("run"));
    target = `/workspaces/${workspaceId}/runs/${runId}`;
    // Cancelling a terminal run is a no-op at the API, so a stale page cannot undo a result.
    await api(session, `/api/v1/workspaces/${workspaceId}/runs/${runId}/cancel`, runSchema, {
      method: "POST",
    });
    return NextResponse.redirect(new URL(target + "?cancelled=1", config.origin), 303);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return NextResponse.redirect(new URL("/login?error=session_expired", config.origin), 303);
    }
    const reason = error instanceof ApiError && error.status === 403 ? "forbidden" : "failed";
    return NextResponse.redirect(new URL(target + "?error=" + reason, config.origin), 303);
  }
}
