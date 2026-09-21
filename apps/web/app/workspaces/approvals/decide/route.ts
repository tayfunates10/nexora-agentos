import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { readConfig, validMutation } from "../../../../lib/auth-core";
import { api, ApiError } from "../../../../lib/server/api";
import { readForm } from "../../../../lib/server/forms";
import { currentSession } from "../../../../lib/server/session";
import { approvalSchema, decisionInput } from "../../../../lib/tool-contracts";

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
    const approvalId = z.uuid().parse(form.get("approval"));
    target = `/workspaces/${workspaceId}/approvals`;
    const parsed = decisionInput.safeParse({ decision: form.get("decision") });
    if (!parsed.success) {
      return NextResponse.redirect(new URL(target + "?error=failed", config.origin), 303);
    }

    // The API revalidates membership, the tool contract and the approval state before it
    // decides, so a stale page cannot approve a call that has since changed or expired.
    await api(session, `/api/v1/workspaces/${workspaceId}/approvals/${approvalId}/decision`,
      approvalSchema, { method: "POST", body: JSON.stringify(parsed.data) });
    return NextResponse.redirect(
      new URL(`${target}?decided=${parsed.data.decision}`, config.origin), 303,
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return NextResponse.redirect(new URL("/login?error=session_expired", config.origin), 303);
    }
    const reason = error instanceof ApiError && error.status === 403 ? "forbidden"
      : error instanceof ApiError && [404, 409].includes(error.status) ? "gone" : "failed";
    return NextResponse.redirect(new URL(target + "?error=" + reason, config.origin), 303);
  }
}
