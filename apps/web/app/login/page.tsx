import { readConfig } from "../../lib/auth-core";
import { PublicShell } from "../../components/shell/PublicShell.tsx";
import { Notice, Panel } from "../../components/ui/primitives.tsx";
import { Icon } from "../../components/ui/Icon.tsx";
import { getTheme, getUi } from "../../lib/i18n/server.ts";

export const dynamic = "force-dynamic";

export default async function Login({ searchParams }: {
  searchParams: Promise<{ error?: string; status?: string }>;
}) {
  const [ui, theme, query] = await Promise.all([getUi(), getTheme(), searchParams]);
  // Sign-in is delegated to the organization's identity provider. There is no password
  // field here to translate, because this console never holds one.
  let configured = false;
  try { configured = !!readConfig(); } catch { /* treated as not configured */ }

  return <PublicShell
    ui={ui}
    theme={theme}
    actions={<a className="link" href="/">{ui.t("auth.platformHealth")}</a>}
  >
    <Panel className="auth-panel enter" labelledBy="login-title">
      <p className="eyebrow">{ui.t("auth.eyebrow")}</p>
      <h1 id="login-title" className="page-title">{ui.t("auth.title")}</h1>
      <p className="notice">{ui.t("auth.intro")}</p>
      {query.error && <Notice live="alert" tone="danger">{ui.t("auth.error.signIn")}</Notice>}
      {query.status === "signed_out"
        && <Notice live="status">{ui.t("auth.status.signedOut")}</Notice>}
      {configured
        ? <form method="post" action="/auth/login">
          <button type="submit" className="button">
            {ui.t("auth.continue")}<Icon name="arrowRight" size={18}/>
          </button>
        </form>
        : <Notice live="status" tone="warning">{ui.t("auth.notConfigured")}</Notice>}
      <p className="field-help">{ui.t("auth.managedNote")}</p>
    </Panel>
  </PublicShell>;
}
