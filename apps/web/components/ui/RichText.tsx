import type { MessageValues } from "../../lib/i18n/icu.ts";
import type { Ui } from "../../lib/i18n/messages.ts";
import type { MessageKey } from "../../messages/en.ts";

// A marker no interface copy contains, used to find where a language wants its link.
const SLOT = "␟";

/**
 * A sentence that contains a link, kept whole in the dictionary. Each language decides
 * where the link falls in its own word order, so nothing is assembled from fragments and
 * no translator has to guess what the pieces will sit next to.
 */
export function TextWithLink({ ui, message, link, href, values }: {
  ui: Ui;
  message: MessageKey;
  link: MessageKey;
  href: string;
  values?: MessageValues;
}) {
  const [before, after] = ui.t(message, { ...values, link: SLOT }).split(SLOT);
  return <>
    {before}
    <a className="link" href={href}>{ui.t(link)}</a>
    {after ?? ""}
  </>;
}
