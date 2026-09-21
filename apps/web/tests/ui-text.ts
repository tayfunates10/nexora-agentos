import { createUi } from "../lib/i18n/messages.ts";
import type { MessageKey } from "../messages/en.ts";
import type { MessageValues } from "../lib/i18n/icu.ts";

/**
 * The browser suite asserts against the dictionary rather than against copy pasted into a
 * test. A wording change then updates one place, while a key that stops existing fails the
 * type check before the browser ever starts.
 */
export const TR = createUi("tr");
export const EN = createUi("en");

/** Turkish is what an unconfigured browser is served, so it is the default under test. */
export function t(key: MessageKey, values?: MessageValues): string {
  return TR.t(key, values);
}
