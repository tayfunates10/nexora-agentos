import type { Metadata } from "next";
import "./globals.css";
import { MotionScript } from "../components/shell/MotionScript.tsx";
import { getLocale, getTheme } from "../lib/i18n/server.ts";
import { createUi } from "../lib/i18n/messages.ts";
import { themeAttribute } from "../lib/i18n/theme.ts";

export async function generateMetadata(): Promise<Metadata> {
  const ui = createUi(await getLocale());
  return {
    title: `${ui.t("common.productName")} | ${ui.t("navigation.overview")}`,
    description: ui.t("landing.intro"),
  };
}

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  // Document language and theme are decided on the server, so the first paint is already
  // correct: there is no flash to correct and no client-side language swap to hydrate.
  const [locale, theme] = await Promise.all([getLocale(), getTheme()]);
  return <html lang={locale} data-theme={themeAttribute(theme)}>
    <head><MotionScript/></head>
    <body>{children}</body>
  </html>;
}
