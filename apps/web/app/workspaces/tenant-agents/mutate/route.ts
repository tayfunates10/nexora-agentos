import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { readConfig, validMutation } from "../../../../lib/auth-core";
import { api, apiNoContent, ApiError } from "../../../../lib/server/api";
import { readForm, TEXT_FORM_BYTES } from "../../../../lib/server/forms";
import { currentSession } from "../../../../lib/server/session";
import {
  channelSchema, forkedAgentSchema, instanceStatusSchema, tenantAgentSchema,
  updateModeSchema, updatePolicySchema,
} from "../../../../lib/catalog-contracts.ts";

const slug = z.string().regex(/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/).min(2).max(64);
const MAX_SETTINGS_KEYS = 50;

/** Tenant settings are a JSON object of plain values, bounded before they are forwarded. */
function settings(raw: string | null): Record<string, unknown> {
  if (raw === null || raw.trim() === "") return {};
  const parsed: unknown = JSON.parse(raw);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("Settings must be a JSON object");
  }
  if (Object.keys(parsed).length > MAX_SETTINGS_KEYS) throw new Error("Too many settings");
  return parsed as Record<string, unknown>;
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
    const catalog = `/workspaces/${workspaceId}/catalog`;
    target = catalog;
    const base = `/api/v1/workspaces/${workspaceId}`;
    const operation = form.get("operation");
    let saved = "1";

    if (operation === "install") {
      const name = form.get("display_name");
      const created = await api(session, base + "/tenant-agents", tenantAgentSchema, {
        method: "POST",
        body: JSON.stringify({
          slug: slug.parse(form.get("slug")),
          display_name: name && name.trim() !== "" ? name.trim().slice(0, 100) : undefined,
        }),
      });
      // Straight to the new agent, where its connections are chosen.
      target = `${catalog}/${created.id}`;
      saved = "added";
    } else if (operation === "policy") {
      await api(session, base + "/agent-update-policy", updatePolicySchema, {
        method: "PUT",
        body: JSON.stringify({
          channel: channelSchema.parse(form.get("channel")),
          mode: updateModeSchema.parse(form.get("mode")),
        }),
      });
    } else {
      const agentId = z.uuid().parse(form.get("agent"));
      const resource = `${base}/tenant-agents/${agentId}`;
      target = `${catalog}/${agentId}`;
      if (operation === "bind") {
        await api(
          session,
          `${resource}/bindings/${slug.parse(form.get("binding"))}`,
          tenantAgentSchema,
          {
            method: "PUT",
            body: JSON.stringify({
              tenant_integration_id: z.uuid().parse(form.get("integration")),
            }),
          },
        );
      } else if (operation === "unbind") {
        await api(
          session,
          `${resource}/bindings/${slug.parse(form.get("binding"))}`,
          tenantAgentSchema,
          { method: "DELETE" },
        );
      } else if (operation === "update") {
        await api(session, resource + "/update", tenantAgentSchema, {
          method: "POST", body: JSON.stringify({}),
        });
      } else if (operation === "rollback") {
        await api(session, resource + "/rollback", tenantAgentSchema, { method: "POST" });
      } else if (operation === "status") {
        await api(session, resource, tenantAgentSchema, {
          method: "PATCH",
          body: JSON.stringify({ status: instanceStatusSchema.parse(form.get("status")) }),
        });
      } else if (operation === "settings") {
        const override = form.get("instructions_override") ?? "";
        await api(session, resource, tenantAgentSchema, {
          method: "PATCH",
          body: JSON.stringify({
            display_name: z.string().trim().min(1).max(100).parse(form.get("display_name")),
            instructions_override: override.trim() === "" ? null : override.slice(0, 20000),
            settings: settings(form.get("settings")),
          }),
        });
      } else if (operation === "fork") {
        await api(session, resource + "/fork", forkedAgentSchema, {
          method: "POST",
          body: JSON.stringify({
            name: z.string().trim().min(1).max(100).parse(form.get("name")),
          }),
        });
        saved = "forked";
      } else if (operation === "remove") {
        await apiNoContent(session, resource, { method: "DELETE" });
        target = catalog;
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
      ? { 403: "forbidden", 409: "conflict", 422: "invalid" }[error.status] ?? "failed"
      : "invalid";
    return NextResponse.redirect(new URL(`${target}?error=${reason}`, config.origin), 303);
  }
}
