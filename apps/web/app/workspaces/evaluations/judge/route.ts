import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { readConfig, validMutation } from "../../../../lib/auth-core";
import { evalJudgeRunSchema } from "../../../../lib/evaluation-contracts";
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
      request.headers.get("origin"),
      config,
      form.get("csrf") ?? "",
      session.csrf,
      request.headers.get("referer"),
    )) return new NextResponse("Forbidden", { status: 403 });

    const workspaceId = z.uuid().parse(form.get("workspace"));
    const evalRunId = z.uuid().parse(form.get("eval_run"));
    const idempotencyKey = z.uuid().parse(form.get("idempotency_key"));
    target = `/workspaces/${workspaceId}/evaluations/runs/${evalRunId}`;

    await api(
      session,
      `/api/v1/workspaces/${workspaceId}/eval-runs/${evalRunId}/judge-runs`,
      evalJudgeRunSchema,
      { method: "POST", headers: { "Idempotency-Key": idempotencyKey } },
    );
    return NextResponse.redirect(new URL(target + "?judge=queued", config.origin), 303);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return NextResponse.redirect(new URL("/login?error=session_expired", config.origin), 303);
    }
    const reason = error instanceof ApiError && error.status === 422 ? "unavailable" : "failed";
    return NextResponse.redirect(new URL(target + "?judge_error=" + reason, config.origin), 303);
  }
}
