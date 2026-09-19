import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { readConfig, validMutation } from "../../../lib/auth-core";
import { currentSession } from "../../../lib/server/session";
import { readForm } from "../../../lib/server/forms";
import { api, ApiError } from "../../../lib/server/api";
import { workspaceInput, memberInput, workspaceSchema } from "../../../lib/workspace-contracts";
export async function POST(request: NextRequest) {
  let config;
  try { config = readConfig(); } catch { return new NextResponse("Unavailable", { status: 503 }); }
  if (!config) return new NextResponse("Unavailable", { status: 503 });
  let target = "/workspaces";
  try {
    const session = await currentSession();
    if (!session) return NextResponse.redirect(new URL("/login", config.origin), 303);
    const form = await readForm(request);
    if (!validMutation(request.headers.get("origin"), config, form.get("csrf") ?? "", session.csrf, request.headers.get("referer"))) return new NextResponse("Forbidden", { status: 403 });
    const operation = form.get("operation");
    if (operation === "create") {
      const body = workspaceInput.parse({ name: form.get("name") });
      const result = await api(session, "/api/v1/workspaces", workspaceSchema, { method: "POST", body: JSON.stringify(body) });
      target += "/" + result.id;
    } else {
      const id = z.uuid().parse(form.get("workspace")); target += "/" + id;
      if (operation === "rename") await api(session, "/api/v1/workspaces/" + id, workspaceSchema,
        { method: "PATCH", body: JSON.stringify(workspaceInput.parse({ name: form.get("name") })) });
      else if (operation === "member") await api(session, "/api/v1/workspaces/" + id + "/members", memberInput,
        { method: "PUT", body: JSON.stringify(memberInput.parse({ subject: form.get("subject"), role: form.get("role") })) });
      else throw new Error("Invalid operation");
    }
    return NextResponse.redirect(new URL(target + "?saved=1", config.origin), 303);
  } catch (error) {
    const path = error instanceof ApiError && error.status === 401 ? "/login?error=session_expired" : target + "?error=save_failed";
    return NextResponse.redirect(new URL(path, config.origin), 303);
  }
}
