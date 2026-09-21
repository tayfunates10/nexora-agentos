"use client";
import { useState } from "react";
import { Icon, type IconName } from "../ui/Icon.tsx";
import { THEMES, THEME_COOKIE, THEME_COOKIE_MAX_AGE, type Theme } from "../../lib/i18n/theme.ts";

const ICONS: Record<Theme, IconName> = { system: "system", light: "light", dark: "dark" };

/**
 * A plain form that posts the choice, enhanced in the browser to apply it without a round
 * trip. Without JavaScript the POST still works; with it, nothing on the page reloads, so
 * an open form, an expanded record and the scroll position all survive the switch.
 */
export function ThemeControl({ label, current, labels }: {
  label: string; current: Theme; labels: Record<Theme, string>;
}) {
  const [theme, setTheme] = useState<Theme>(current);

  function apply(event: React.MouseEvent<HTMLButtonElement>, value: Theme) {
    if (typeof document === "undefined") return;
    event.preventDefault();
    setTheme(value);
    const root = document.documentElement;
    if (value === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", value);
    const secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie =
      `${THEME_COOKIE}=${value}; Path=/; Max-Age=${THEME_COOKIE_MAX_AGE}; SameSite=Lax${secure}`;
  }

  return <form className="field" method="post" action="/preferences">
    <p className="field-label" id="theme-control-label">{label}</p>
    <div className="button-row" role="group" aria-labelledby="theme-control-label">
      {THEMES.map(value => <button
        key={value}
        type="submit"
        name="theme"
        value={value}
        className={"button small " + (value === theme ? "" : "secondary")}
        aria-pressed={value === theme}
        onClick={event => apply(event, value)}
      >
        <Icon name={ICONS[value]} size={16}/>{labels[value]}
      </button>)}
    </div>
  </form>;
}
