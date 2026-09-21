import "server-only";
import type { Session } from "../auth-core";
import type { ConsoleChrome } from "../../components/shell/ConsoleShell.tsx";
import { getTheme, getUi } from "../i18n/server.ts";
import { api } from "./api";
import { pageSchema, type Workspace } from "../workspace-contracts";

/**
 * Everything the application frame needs, resolved once per page render: the reader's
 * language, their theme, and the workspaces they can actually reach.
 *
 * The switcher lists real memberships. When that list cannot be read the frame is told so
 * explicitly and falls back to the current workspace plus a link, rather than showing a
 * control that offers nothing.
 */
export async function consoleChrome(
  session: Session, workspace: Workspace | null = null,
): Promise<ConsoleChrome> {
  const [ui, theme] = await Promise.all([getUi(), getTheme()]);
  let workspaces: Workspace[] | null = null;
  if (workspace) {
    try {
      workspaces = (await api(session, "/api/v1/workspaces?limit=25", pageSchema)).items;
    } catch {
      // An unreadable list is a degraded switcher, never a failed page.
      workspaces = null;
    }
  }
  return { ui, theme, csrf: session.csrf, workspace, workspaces };
}
