import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { readConfig, validMutation } from "../../../../lib/auth-core";
import { SOURCE_KEY } from "../../../../lib/knowledge-contracts";
import { apiNoContent, ApiError } from "../../../../lib/server/api";
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
    target = `/workspaces/${workspaceId}/knowledge`;
    const source = z.string().min(1).max(255).regex(SOURCE_KEY).safeParse(form.get("source"));
    if (!source.success) {
      return NextResponse.redirect(new URL(target + "?error=invalid", config.origin), 303);
    }

    // Deletion answers 204 with no body. A source whose ingestion is currently running
    // answers 409 instead of racing the worker, and the console says so.
    await apiNoContent(
      session,
      `/api/v1/workspaces/${workspaceId}/knowledge/sources/${encodeURIComponent(source.data)}`,
      { method: "DELETE" },
    );
    return NextResponse.redirect(new URL(target + "?saved=deleted", config.origin), 303);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return NextResponse.redirect(new URL("/login?error=session_expired", config.origin), 303);
    }
    const reason = error instanceof ApiError && error.status === 403 ? "forbidden"
      : error instanceof ApiError && error.status === 409 ? "busy" : "failed";
    return NextResponse.redirect(new URL(target + "?error=" + reason, config.origin), 303);
  }
}
