import { NextRequest, NextResponse } from "next/server";
import { beginLogin, cookieNames, cookieOptions, readConfig, validRequestSource } from "../../../lib/auth-core";
import { sessionStore } from "../../../lib/server/redis";
export async function POST(request: NextRequest) {
  try {
    const config = readConfig();
    if (!config) return new NextResponse("Sign-in is not configured", { status: 503 });
    if (!validRequestSource(request.headers.get("origin"), request.headers.get("referer"), config)) return new NextResponse("Forbidden", { status: 403 });
    const { id, url } = await beginLogin(config, sessionStore);
    const response = NextResponse.redirect(url, 303);
    response.cookies.set(cookieNames(config).flow, id, cookieOptions(config, 600));
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch { return new NextResponse("Sign-in is temporarily unavailable", { status: 503 }); }
}
