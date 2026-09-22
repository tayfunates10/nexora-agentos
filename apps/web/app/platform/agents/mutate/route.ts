import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { readConfig, validMutation } from "../../../../lib/auth-core";
import { api, ApiError } from "../../../../lib/server/api";
import { readForm, TEXT_FORM_BYTES } from "../../../../lib/server/forms";
import { currentSession } from "../../../../lib/server/session";
import {
  catalogAgentInput, catalogAgentSchema, parseManifest, rolloutInput, rolloutSchema,
} from "../../../../lib/studio-contracts.ts";

const slug = z.string().regex(/^[a-z][a-z0-9]*(-[a-z0-9]+)*$/).min(3).max(64);
const TARGET = "/platform/agents";

// A version is published whole and the platform applies its own contract to it, so the
// response is only checked for the fields this handler reports on.
const publishedSchema = z.object({ version: z.string().min(5).max(32) }).loose();

export async function POST(request: NextRequest) {
  let config;
  try { config = readConfig(); } catch { return new NextResponse("Unavailable", { status: 503 }); }
  if (!config) return new NextResponse("Unavailable", { status: 503 });

  try {
    const session = await currentSession();
    if (!session) return NextResponse.redirect(new URL("/login", config.origin), 303);
    const form = await readForm(request, TEXT_FORM_BYTES);
    if (!validMutation(
      request.headers.get("origin"), config, form.get("csrf") ?? "", session.csrf,
      request.headers.get("referer"),
    )) return new NextResponse("Forbidden", { status: 403 });

    const operation = form.get("operation");
    let saved = "1";

    if (operation === "create") {
      const body = catalogAgentInput.parse({
        slug: form.get("slug"),
        name: form.get("name"),
        description: form.get("description"),
        category: form.get("category"),
        icon: form.get("icon"),
        status: form.get("status"),
        visibility: form.get("visibility"),
      });
      await api(session, "/api/v1/platform/agents", catalogAgentSchema, {
        method: "POST", body: JSON.stringify(body),
      });
    } else {
      const name = slug.parse(form.get("slug"));
      const base = `/api/v1/platform/agents/${name}`;
      if (operation === "publish") {
        let manifest;
        try {
          manifest = parseManifest(form.get("manifest") ?? "", name);
        } catch {
          return NextResponse.redirect(new URL(TARGET + "?error=manifest", config.origin), 303);
        }
        await api(session, base + "/versions", publishedSchema, {
          method: "POST", body: JSON.stringify({ manifest }),
        });
        saved = "published";
      } else if (operation === "rollout") {
        await api(session, base + "/rollouts", rolloutSchema, {
          method: "POST",
          body: JSON.stringify(rolloutInput.parse({
            version: form.get("version"),
            channel: form.get("channel"),
            percentage: form.get("percentage"),
          })),
        });
      } else if (operation === "widen" || operation === "pause") {
        const rollout = z.uuid().parse(form.get("rollout"));
        await api(session, `${base}/rollouts/${rollout}`, rolloutSchema, {
          method: "PATCH",
          body: JSON.stringify(operation === "pause"
            ? { state: "paused" }
            : { percentage: z.coerce.number().int().min(0).max(100)
              .parse(form.get("percentage")) }),
        });
      } else if (operation === "rollback") {
        const rollout = z.uuid().parse(form.get("rollout"));
        await api(session, `${base}/rollouts/${rollout}/rollback`, rolloutSchema, {
          method: "POST",
        });
      } else if (operation === "grant") {
        await api(session, base + "/entitlements", z.object({ workspace_id: z.uuid() }).loose(), {
          method: "PUT",
          body: JSON.stringify({ workspace_id: z.uuid().parse(form.get("workspace")) }),
        });
      } else {
        throw new Error("Invalid operation");
      }
    }
    return NextResponse.redirect(new URL(`${TARGET}?saved=${saved}`, config.origin), 303);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return NextResponse.redirect(new URL("/login?error=session_expired", config.origin), 303);
    }
    const reason = error instanceof ApiError
      ? { 403: "forbidden", 404: "forbidden", 409: "conflict", 422: "invalid" }[error.status]
        ?? "failed"
      : "invalid";
    return NextResponse.redirect(new URL(`${TARGET}?error=${reason}`, config.origin), 303);
  }
}
