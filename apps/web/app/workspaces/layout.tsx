import { redirect } from "next/navigation";
import { currentSession } from "../../lib/server/session";
import { getTheme, getUi } from "../../lib/i18n/server.ts";
import { PublicShell } from "../../components/shell/PublicShell.tsx";
import { Notice, Panel } from "../../components/ui/primitives.tsx";

export const dynamic = "force-dynamic";

/**
 * The signed-in boundary. Each page below renders its own application frame, because the
 * frame needs the workspace the page resolved; this layout only proves there is a session
 * to render anything with.
 */
export default async function WorkspaceLayout({ children }: { children: React.ReactNode }) {
  let session;
  try {
    session = await currentSession();
  } catch {
    const [ui, theme] = await Promise.all([getUi(), getTheme()]);
    return <PublicShell ui={ui} theme={theme}>
      <Panel className="auth-panel" labelledBy="session-error-title">
        <h1 id="session-error-title" className="page-title">
          {ui.t("auth.sessionUnavailableTitle")}</h1>
        <Notice live="alert" tone="danger">{ui.t("auth.sessionUnavailableBody")}</Notice>
        <p><a className="link" href="/">{ui.t("auth.backToHealth")}</a></p>
      </Panel>
    </PublicShell>;
  }
  if (!session) redirect("/login");
  return children;
}
