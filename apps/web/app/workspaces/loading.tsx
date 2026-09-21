import { getUi } from "../../lib/i18n/server.ts";

export default async function Loading() {
  const ui = await getUi();
  return <main className="content" role="status" aria-live="polite">
    <h1 className="page-title">{ui.t("workspaces.loading")}</h1>
  </main>;
}
