import { getUi } from "../lib/i18n/server.ts";

export default async function Loading() {
  const ui = await getUi();
  return <main className="public-main" role="status" aria-live="polite">
    <div className="card panel auth-panel">
      <h1 className="page-title">{ui.t("common.productName")}</h1>
      <p className="notice">{ui.t("health.checking")}</p>
    </div>
  </main>;
}
