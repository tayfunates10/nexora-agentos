import "server-only";
import { cookies, headers } from "next/headers";
import { createUi, type Ui } from "./messages.ts";
import { LOCALE_COOKIE, resolveLocale, type Locale } from "./locale.ts";
import { THEME_COOKIE, resolveTheme, type Theme } from "./theme.ts";

/**
 * The locale is resolved per request. Nothing is cached in a module-level variable, so two
 * concurrent requests in different languages can never be served each other's copy.
 */
export async function getLocale(): Promise<Locale> {
  const store = await cookies();
  const stored = store.get(LOCALE_COOKIE)?.value;
  // The header is only consulted when no valid preference is stored, which keeps a chosen
  // language stable even on a browser that asks for something else.
  if (stored) return resolveLocale(stored, null);
  return resolveLocale(null, (await headers()).get("accept-language"));
}

export async function getTheme(): Promise<Theme> {
  return resolveTheme((await cookies()).get(THEME_COOKIE)?.value);
}

export async function getUi(): Promise<Ui> {
  return createUi(await getLocale());
}
