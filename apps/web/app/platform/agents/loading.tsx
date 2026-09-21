import { getUi } from "../../../lib/i18n/server.ts";

export default async function Loading() {
  const ui = await getUi();
  return <main className="content" role="status" aria-live="polite">
    <p className="page-intro">{ui.t("studio.loading")}</p>
  </main>;
}
