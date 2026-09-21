import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { readConfig, validMutation } from "../../../../lib/auth-core";
import { ingestionSchema, sourceInput } from "../../../../lib/knowledge-contracts";
import { api, ApiError } from "../../../../lib/server/api";
import { readForm, TEXT_FORM_BYTES } from "../../../../lib/server/forms";
import { currentSession } from "../../../../lib/server/session";

// A source document is the largest body the console accepts: 500k characters of UTF-8 plus
// form encoding. The bound is explicit rather than inherited from the small settings default.
const SOURCE_FORM_BYTES = 2_000_000;

export async function POST(request: NextRequest) {
  let config;
  try { config = readConfig(); } catch { return new NextResponse("Unavailable", { status: 503 }); }
  if (!config) return new NextResponse("Unavailable", { status: 503 });

  let target = "/workspaces";
  try {
    const session = await currentSession();
    if (!session) return NextResponse.redirect(new URL("/login", config.origin), 303);
    const form = await readForm(request, Math.max(SOURCE_FORM_BYTES, TEXT_FORM_BYTES));
    if (!validMutation(
      request.headers.get("origin"), config, form.get("csrf") ?? "", session.csrf,
      request.headers.get("referer"),
    )) return new NextResponse("Forbidden", { status: 403 });

    const workspaceId = z.uuid().parse(form.get("workspace"));
    target = `/workspaces/${workspaceId}/knowledge`;
    const parsed = sourceInput.safeParse({
      source_key: form.get("source_key"), version: form.get("version"),
      title: form.get("title"), text: form.get("text"),
      access_scope: form.get("access_scope"),
    });
    const idempotencyKey = z.uuid().safeParse(form.get("idempotency"));
    if (!parsed.success || !idempotencyKey.success) {
      return NextResponse.redirect(new URL(target + "?error=invalid", config.origin), 303);
    }

    const job = await api(session, `/api/v1/workspaces/${workspaceId}/knowledge/sources`,
      ingestionSchema, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey.data },
        body: JSON.stringify(parsed.data),
      });
    return NextResponse.redirect(
      new URL(`${target}?saved=queued&job=${job.id}`, config.origin), 303,
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return NextResponse.redirect(new URL("/login?error=session_expired", config.origin), 303);
    }
    const reason = error instanceof ApiError && error.status === 403 ? "forbidden"
      : error instanceof ApiError && error.status === 409 ? "busy" : "failed";
    return NextResponse.redirect(new URL(target + "?error=" + reason, config.origin), 303);
  }
}
