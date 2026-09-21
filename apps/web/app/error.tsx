"use client";
import { useState } from "react";
import { createUi } from "../lib/i18n/messages.ts";
import { DEFAULT_LOCALE, isLocale } from "../lib/i18n/locale.ts";

/**
 * The last-resort boundary. It cannot read a cookie, so it takes the language the server
 * already stamped on the document, which is the same preference every other page used.
 */
export default function ErrorPage({ reset }: { reset: () => void }) {
  const [ui] = useState(() => {
    const lang = typeof document === "undefined" ? null : document.documentElement.lang;
    return createUi(isLocale(lang) ? lang : DEFAULT_LOCALE);
  });
  return <main className="public-main">
    <div className="card panel auth-panel" role="alert">
      <h1 className="page-title">{ui.t("errors.appTitle")}</h1>
      <p className="notice">{ui.t("errors.appBody")}</p>
      <button type="button" className="button" onClick={reset}>{ui.t("common.tryAgain")}</button>
    </div>
  </main>;
}
