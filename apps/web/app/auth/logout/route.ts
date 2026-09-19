import { NextRequest, NextResponse } from "next/server";
import { cookieNames, cookieOptions, endSession, loadSession, readConfig, validMutation, validRequestSource } from "../../../lib/auth-core";
import { sessionStore } from "../../../lib/server/redis";
import { readForm } from "../../../lib/server/forms";
export async function POST(request: NextRequest) {
  try {
    const config = readConfig(); if (!config) return new NextResponse("Unavailable", { status: 503 });
    const id = request.cookies.get(cookieNames(config).session)?.value;
    const session = await loadSession(sessionStore, id);
    const form = await readForm(request);
    const origin = request.headers.get("origin"); const referer = request.headers.get("referer");
    if (!validRequestSource(origin, referer, config) || (session && !validMutation(origin, config, form.get("csrf") ?? "", session.csrf, referer))) return new NextResponse("Forbidden", { status: 403 });
    await endSession(sessionStore, id);
    const response = NextResponse.redirect(new URL("/login?status=signed_out", config.origin), 303);
    response.cookies.set(cookieNames(config).session, "", cookieOptions(config, 0));
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch { return new NextResponse("Sign-out unavailable. Please retry.", { status: 503 }); }
}
