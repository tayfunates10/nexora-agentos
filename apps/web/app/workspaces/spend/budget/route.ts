import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { readConfig, validMutation } from "../../../../lib/auth-core";
import { api, ApiError } from "../../../../lib/server/api";
import { readForm } from "../../../../lib/server/forms";
import { currentSession } from "../../../../lib/server/session";
import {
  budgetSchema,
  enforcementSchema,
  parseThresholds,
  unitsToMicros,
} from "../../../../lib/spend-contracts";

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
    target = `/workspaces/${workspaceId}/spend`;
    // The browser submits accounting units; the API stores exact micros.
    let monthlyLimitMicros: number;
    try {
      monthlyLimitMicros = unitsToMicros(form.get("limit") ?? "");
    } catch {
      return NextResponse.redirect(new URL(target + "?budget_error=invalid", config.origin), 303);
    }
    const enforcement = enforcementSchema.parse(form.get("enforcement"));
    let alertThresholds: number[];
    try {
      alertThresholds = parseThresholds(form.getAll("threshold"));
    } catch {
      return NextResponse.redirect(new URL(target + "?budget_error=thresholds", config.origin), 303);
    }

    await api(session, `/api/v1/workspaces/${workspaceId}/spend/budget`, budgetSchema, {
      method: "PUT",
      body: JSON.stringify({
        monthly_limit_micros: monthlyLimitMicros,
        enforcement,
        alert_thresholds: alertThresholds,
      }),
    });
    return NextResponse.redirect(new URL(target + "?saved=budget", config.origin), 303);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return NextResponse.redirect(new URL("/login?error=session_expired", config.origin), 303);
    }
    const reason = error instanceof ApiError && error.status === 403 ? "forbidden" : "failed";
    return NextResponse.redirect(new URL(target + "?budget_error=" + reason, config.origin), 303);
  }
}
