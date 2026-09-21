"use client";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Icon } from "../ui/Icon.tsx";
import {
  LOCALES, LOCALE_COOKIE, LOCALE_COOKIE_MAX_AGE, LOCALE_NAMES, type Locale,
} from "../../lib/i18n/locale.ts";

/**
 * The language switch keeps the reader where they are. With JavaScript it writes the
 * preference and asks the server for the same route again, so the URL, its filters and
 * cursor, the open record and anything typed into a form stay exactly as they were; no
 * POST is replayed and no queued work is started again. Without JavaScript the form posts
 * and the handler returns to the same path.
 */
export function LanguageControl({ label, current }: { label: string; current: Locale }) {
  const router = useRouter();
  const [locale, setLocale] = useState<Locale>(current);
  const [pending, startTransition] = useTransition();

  function apply(event: React.MouseEvent<HTMLButtonElement>, value: Locale) {
    if (typeof document === "undefined") return;
    event.preventDefault();
    setLocale(value);
    const secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie =
      `${LOCALE_COOKIE}=${value}; Path=/; Max-Age=${LOCALE_COOKIE_MAX_AGE}; SameSite=Lax${secure}`;
    startTransition(() => router.refresh());
  }

  return <form className="field" method="post" action="/preferences">
    <p className="field-label" id="language-control-label">
      <Icon name="language" size={14}/> {label}
    </p>
    <div className="button-row" role="group" aria-labelledby="language-control-label">
      {LOCALES.map(value => <button
        key={value}
        type="submit"
        name="locale"
        value={value}
        lang={value}
        className={"button small " + (value === locale ? "" : "secondary")}
        aria-pressed={value === locale}
        aria-busy={pending && value === locale ? true : undefined}
        onClick={event => apply(event, value)}
      >{LOCALE_NAMES[value]}</button>)}
    </div>
  </form>;
}
