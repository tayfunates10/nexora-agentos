import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { readConfig, validMutation } from "../../../../lib/auth-core";
import { api, apiNoContent, ApiError } from "../../../../lib/server/api";
import { readForm, TEXT_FORM_BYTES } from "../../../../lib/server/forms";
import { currentSession } from "../../../../lib/server/session";
import {
  connectionTestSchema, integrationSchema,
} from "../../../../lib/integration-contracts.ts";

const oauthStartSchema = z.object({
  integration_id: z.uuid(),
  authorization_url: z.url(),
  state: z.string().min(16).max(128),
});

const identifier = z.string().regex(/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/).min(2).max(64);
const FIELD = /^field_[a-z][a-z0-9_]{0,63}$/;

/**
 * Credential values reach the platform through this handler and go no further. They are
 * read from the submitted form, forwarded once to the API, and never written to a
 * redirect target, a cookie or a log line — only the outcome is.
 */
function credentials(form: URLSearchParams): Record<string, string> {
  const values: Record<string, string> = {};
  for (const [name, value] of form.entries()) {
    if (FIELD.test(name) && value !== "") values[name.slice("field_".length)] = value;
  }
  return values;
}

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
    target = `/workspaces/${workspaceId}/integrations`;
    const base = `/api/v1/workspaces/${workspaceId}/integrations`;
    const operation = form.get("operation");
    let saved = "updated";

    if (operation === "connect") {
      await api(session, base, integrationSchema, {
        method: "POST",
        body: JSON.stringify({
          integration_definition_id: identifier.parse(form.get("definition")),
          display_name: z.string().trim().min(1).max(100).parse(form.get("display_name")),
          account_identifier: z.string().trim().min(1).max(200)
            .parse(form.get("account_identifier")),
          credentials: credentials(form),
        }),
      });
      saved = "connected";
    } else if (operation === "oauth") {
      const started = await api(session, base + "/oauth/start", oauthStartSchema, {
        method: "POST",
        body: JSON.stringify({
          integration_definition_id: identifier.parse(form.get("definition")),
          display_name: z.string().trim().min(1).max(100).parse(form.get("display_name")),
          account_identifier: z.string().trim().min(1).max(200)
            .parse(form.get("account_identifier")),
          scopes: [],
        }),
      });
      // The provider's own authorization page is the next step; nothing about the
      // credential exists yet, so there is nothing to keep here.
      return NextResponse.redirect(started.authorization_url, 303);
    } else {
      const integrationId = z.uuid().parse(form.get("integration"));
      const resource = `${base}/${integrationId}`;
      if (operation === "rotate") {
        await api(session, resource + "/credential", integrationSchema, {
          method: "PUT", body: JSON.stringify({ credentials: credentials(form) }),
        });
        saved = "rotated";
      } else if (operation === "toggle") {
        await api(session, resource, integrationSchema, {
          method: "PATCH",
          body: JSON.stringify({ enabled: form.get("enabled") === "true" }),
        });
      } else if (operation === "test") {
        await api(session, resource + "/test", connectionTestSchema, { method: "POST" });
        saved = "tested";
      } else if (operation === "disconnect") {
        await apiNoContent(session, resource, { method: "DELETE" });
        saved = "removed";
      } else {
        throw new Error("Invalid operation");
      }
    }
    return NextResponse.redirect(new URL(`${target}?saved=${saved}`, config.origin), 303);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return NextResponse.redirect(new URL("/login?error=session_expired", config.origin), 303);
    }
    const reason = error instanceof ApiError
      ? { 403: "forbidden", 409: "conflict", 422: "invalid", 503: "vault" }[error.status] ?? "failed"
      : "invalid";
    return NextResponse.redirect(new URL(`${target}?error=${reason}`, config.origin), 303);
  }
}
