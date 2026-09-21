// Supported interface languages. The identifiers are an allowlist: a locale that is not
// in this tuple never reaches dictionary loading, so a cookie or header value can never
// select a file by name.
export const LOCALES = ["tr", "en"] as const;
export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "tr";
export const LOCALE_COOKIE = "nexora_locale";
// One year. The preference is a display choice, never combined with the session cookie.
export const LOCALE_COOKIE_MAX_AGE = 31_536_000;

// The BCP 47 tags used for Intl formatting. They are display formats, not identifiers:
// the API contract, enums and stored data stay untouched by the interface language.
export const LOCALE_TAGS: Record<Locale, string> = { tr: "tr-TR", en: "en-GB" };

// Each language names itself, so the control is readable whichever language is active.
export const LOCALE_NAMES: Record<Locale, string> = { tr: "Türkçe", en: "English" };

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value);
}

/**
 * Resolve a base language from a full tag: tr-TR becomes tr, en-US becomes en. Anything
 * this platform does not serve returns null rather than falling through to a guess.
 */
export function baseLanguage(tag: string): Locale | null {
  const base = tag.trim().toLowerCase().split("-")[0];
  return isLocale(base) ? base : null;
}

/**
 * Pick the best supported language from an Accept-Language header.
 *
 * Quality weights decide the order and q=0 is an explicit refusal, so a header such as
 * `tr;q=0, en` selects English rather than the first tag it happens to mention.
 */
export function negotiateLocale(header: string | null | undefined): Locale | null {
  if (!header) return null;
  const candidates: { locale: Locale; quality: number; index: number }[] = [];
  const refused = new Set<Locale>();
  header.split(",").forEach((part, index) => {
    const [rawTag, ...parameters] = part.split(";");
    const tag = rawTag.trim();
    if (!tag) return;
    let quality = 1;
    for (const parameter of parameters) {
      const [name, value] = parameter.split("=").map(piece => piece.trim());
      if (name?.toLowerCase() !== "q") continue;
      const parsed = Number(value);
      quality = Number.isFinite(parsed) ? Math.min(1, Math.max(0, parsed)) : 0;
    }
    const locale = tag === "*" ? DEFAULT_LOCALE : baseLanguage(tag);
    if (!locale) return;
    if (quality === 0) { refused.add(locale); return; }
    candidates.push({ locale, quality, index });
  });
  const best = candidates
    .filter(candidate => !refused.has(candidate.locale))
    .sort((a, b) => b.quality - a.quality || a.index - b.index)[0];
  return best?.locale ?? null;
}

/**
 * The preference order the product promises: a stored valid choice, then the first
 * supported Accept-Language preference of the request, then Turkish.
 */
export function resolveLocale(
  cookieValue: string | null | undefined,
  acceptLanguage: string | null | undefined,
): Locale {
  if (isLocale(cookieValue)) return cookieValue;
  return negotiateLocale(acceptLanguage) ?? DEFAULT_LOCALE;
}
