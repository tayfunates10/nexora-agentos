import { NextRequest, NextResponse } from "next/server";
import { cookieNames, cookieOptions, endSession, finishLogin, readConfig } from "../../../lib/auth-core";
import { sessionStore } from "../../../lib/server/redis";
export async function GET(request: NextRequest) {
  let config;
  try { config = readConfig(); } catch { return new NextResponse("Sign-in unavailable", { status: 503 }); }
  if (!config) return new NextResponse("Sign-in unavailable", { status: 503 });
  const names = cookieNames(config);
  let response;
  try {
    // Use only the configured origin; proxy Host headers cannot change the redirect URI.
    const callback = new URL("/auth/callback" + request.nextUrl.search, config.origin);
    const result = await finishLogin(config, sessionStore, request.cookies.get(names.flow)?.value ?? "", callback);
    await endSession(sessionStore, request.cookies.get(names.session)?.value);
    response = NextResponse.redirect(new URL("/workspaces", config.origin), 303);
    response.cookies.set(names.session, result.id, cookieOptions(config, result.ttl));
  } catch {
    response = NextResponse.redirect(new URL("/login?error=login_failed", config.origin), 303);
  }
  response.cookies.set(names.flow, "", cookieOptions(config, 0));
  response.headers.set("Cache-Control", "no-store");
  response.headers.set("Referrer-Policy", "no-referrer");
  return response;
}
