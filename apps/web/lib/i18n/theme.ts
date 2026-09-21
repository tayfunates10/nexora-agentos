// Theme is independent of language: all four combinations are equally supported.
export const THEMES = ["system", "light", "dark"] as const;
export type Theme = (typeof THEMES)[number];

export const DEFAULT_THEME: Theme = "system";
export const THEME_COOKIE = "nexora_theme";
export const THEME_COOKIE_MAX_AGE = 31_536_000;

export function isTheme(value: unknown): value is Theme {
  return typeof value === "string" && (THEMES as readonly string[]).includes(value);
}

export function resolveTheme(cookieValue: string | null | undefined): Theme {
  return isTheme(cookieValue) ? cookieValue : DEFAULT_THEME;
}

/**
 * The attribute the server renders on <html>. "system" deliberately renders nothing so
 * the stylesheet's prefers-color-scheme rules decide, which keeps the first paint and
 * the first client render identical without an inline script.
 */
export function themeAttribute(theme: Theme): "light" | "dark" | undefined {
  return theme === "system" ? undefined : theme;
}
