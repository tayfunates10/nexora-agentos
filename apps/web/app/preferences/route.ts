import { NextRequest, NextResponse } from "next/server";
import { readConfig, validRequestSource } from "../../lib/auth-core";
import { readForm } from "../../lib/server/forms";
import { LOCALE_COOKIE, LOCALE_COOKIE_MAX_AGE, isLocale } from "../../lib/i18n/locale.ts";
import { THEME_COOKIE, THEME_COOKIE_MAX_AGE, isTheme } from "../../lib/i18n/theme.ts";

// Display preferences, submitted without JavaScript. They carry no authority: the cookies
// are read only to choose copy and colours, and are never combined with the session.
function configured() {
  try { return readConfig(); } catch { return null; }
}

function fromThisSite(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  const referer = request.headers.get("referer");
  const config = configured();
  if (config) return validRequestSource(origin, referer, config);
  // Before sign-in is configured there is no declared origin to compare against, so the
  // browser's own Origin header has to match the host it addressed.
  if (!origin) return false;
  try { return new URL(origin).host === request.headers.get("host"); } catch { return false; }
}

/** Return to the page the reader was on, and only ever to a path on this site. */
function backTo(request: NextRequest): URL {
  const base = configured()?.origin ?? request.nextUrl.origin;
  const referer = request.headers.get("referer");
  if (referer) {
    try {
      const target = new URL(referer);
      if (target.origin === new URL(base).origin && target.pathname !== "/preferences") {
        return new URL(target.pathname + target.search, base);
      }
    } catch { /* fall through to the site root */ }
  }
  return new URL("/", base);
}

export async function POST(request: NextRequest) {
  try {
    if (!fromThisSite(request)) return new NextResponse("Forbidden", { status: 403 });
    const form = await readForm(request);
    const response = NextResponse.redirect(backTo(request), 303);
    const secure = (configured()?.origin ?? request.nextUrl.origin).startsWith("https:");
    const options = { path: "/", sameSite: "lax" as const, secure, httpOnly: false };

    // Only an allowlisted value is ever stored, so a crafted cookie cannot reach a
    // dictionary or a stylesheet by name.
    const locale = form.get("locale");
    if (locale !== null) {
      if (!isLocale(locale)) return new NextResponse("Unsupported locale", { status: 400 });
      response.cookies.set(LOCALE_COOKIE, locale, { ...options, maxAge: LOCALE_COOKIE_MAX_AGE });
    }
    const theme = form.get("theme");
    if (theme !== null) {
      if (!isTheme(theme)) return new NextResponse("Unsupported theme", { status: 400 });
      response.cookies.set(THEME_COOKIE, theme, { ...options, maxAge: THEME_COOKIE_MAX_AGE });
    }
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch {
    return new NextResponse("The preference could not be saved", { status: 400 });
  }
}
