import { en, type MessageKey, type Messages } from "../../messages/en.ts";
import { tr } from "../../messages/tr.ts";
import { formatMessage, type MessageValues } from "./icu.ts";
import { LOCALE_TAGS, type Locale } from "./locale.ts";

// A static record, not a lookup by file name: a locale value from a cookie or a header
// can only ever select one of these two objects.
const DICTIONARIES: Record<Locale, Messages> = { tr, en };

export type { MessageKey, Messages };

export function dictionary(locale: Locale): Messages {
  return DICTIONARIES[locale];
}

export class MissingMessageError extends Error {}

/**
 * Resolve one message. A key the dictionary does not carry is fatal outside production,
 * where the automated gate is meant to catch it; in production the reader gets the key's
 * own name rather than a crashed page, and the miss is recorded for the operator.
 */
export function translate(locale: Locale, key: MessageKey, values?: MessageValues): string {
  const message = DICTIONARIES[locale][key];
  if (message === undefined) {
    if (process.env.NODE_ENV !== "production") {
      throw new MissingMessageError(`Missing ${locale} message: ${key}`);
    }
    console.error(JSON.stringify({ event: "i18n.missing_message", locale, key }));
    return key;
  }
  return values ? formatMessage(message, values, locale) : message;
}

export interface Ui {
  locale: Locale;
  tag: string;
  t: (key: MessageKey, values?: MessageValues) => string;
}

export function createUi(locale: Locale): Ui {
  return {
    locale,
    tag: LOCALE_TAGS[locale],
    t: (key, values) => translate(locale, key, values),
  };
}
