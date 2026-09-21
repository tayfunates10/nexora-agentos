import { LOCALE_TAGS, type Locale } from "./locale.ts";
import type { MessageKey } from "../../messages/en.ts";

export type Translate = (key: MessageKey, values?: Record<string, string | number>) => string;

const UTC_TIMESTAMP: Intl.DateTimeFormatOptions = {
  dateStyle: "medium", timeStyle: "short", timeZone: "UTC",
};
const UTC_DATE: Intl.DateTimeFormatOptions = { dateStyle: "medium", timeZone: "UTC" };

/**
 * Timestamps stay in UTC in every language. Changing the interface language changes how a
 * moment is written, never which moment it is, so the recorded instant and the reader's
 * clock can never drift apart silently.
 */
export function formatTimestamp(value: string, locale: Locale): string {
  return new Intl.DateTimeFormat(LOCALE_TAGS[locale], UTC_TIMESTAMP).format(new Date(value)) + " UTC";
}

export function formatDate(value: string, locale: Locale): string {
  return new Intl.DateTimeFormat(LOCALE_TAGS[locale], UTC_DATE).format(new Date(value));
}

export function formatNumber(value: number, locale: Locale): string {
  return new Intl.NumberFormat(LOCALE_TAGS[locale]).format(value);
}

/** A ratio rendered as a percentage in the position the language expects. */
export function formatPercent(ratio: number, locale: Locale, fractionDigits = 1): string {
  return new Intl.NumberFormat(LOCALE_TAGS[locale], {
    style: "percent", minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits,
  }).format(ratio);
}

export function formatIntegerPercent(percent: number, locale: Locale): string {
  return new Intl.NumberFormat(LOCALE_TAGS[locale], { style: "percent" }).format(percent / 100);
}

export function decimalSeparator(locale: Locale): string {
  const parts = new Intl.NumberFormat(LOCALE_TAGS[locale]).formatToParts(1.1);
  return parts.find(part => part.type === "decimal")?.value ?? ".";
}

/** Seconds as a readable duration. A clock skew never renders as a negative span. */
export function formatDuration(totalSeconds: number, t: Translate): string {
  const seconds = Math.max(0, Math.round(totalSeconds));
  if (seconds < 60) return t("format.durationSeconds", { seconds });
  const minutes = Math.floor(seconds / 60);
  return minutes < 60
    ? t("format.durationMinutes", { minutes, seconds: seconds % 60 })
    : t("format.durationHours", { hours: Math.floor(minutes / 60), minutes: minutes % 60 });
}

export function formatMilliseconds(value: number, locale: Locale, t: Translate): string {
  return t("format.milliseconds", { value: formatNumber(value, locale) });
}

/**
 * Quality is reported in milli-units and its delta in percentage points, not as a
 * relative increase: +2.5 points is not a 2.5% improvement and is never written as one.
 */
export function formatQuality(milli: number | null, locale: Locale, t: Translate): string {
  return milli === null ? t("common.empty") : formatPercent(milli / 1000, locale);
}

export function formatQualityDelta(milli: number | null, locale: Locale, t: Translate): string {
  if (milli === null) return t("common.empty");
  const points = milli / 10;
  const signed = new Intl.NumberFormat(LOCALE_TAGS[locale], {
    minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: "exceptZero",
  }).format(points);
  return t("format.points", { value: signed });
}

/**
 * Accounting units are stored as exact integer micros. The whole part is grouped by the
 * language's own rules and joined with its decimal separator, so no step of the display
 * passes through a float that could lose a millionth.
 */
export function formatUnitsFrom(canonical: string, locale: Locale): string {
  const [whole, fraction] = canonical.split(".");
  const grouped = new Intl.NumberFormat(LOCALE_TAGS[locale]).format(BigInt(whole));
  return fraction === undefined ? grouped : grouped + decimalSeparator(locale) + fraction;
}

/**
 * The editable limit field carries no thousands separator: a grouped value would be
 * ambiguous against the decimal separator it shares with other languages.
 */
export function toLimitInput(canonical: string, locale: Locale): string {
  return canonical.replace(".", decimalSeparator(locale));
}
