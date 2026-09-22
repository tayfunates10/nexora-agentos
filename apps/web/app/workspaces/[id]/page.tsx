import { redirect } from "next/navigation";
import { z } from "zod";
import { currentSession } from "../../../lib/server/session";
import { api, ApiError } from "../../../lib/server/api";
import { workspaceSchema } from "../../../lib/workspace-contracts";
import { consoleChrome } from "../../../lib/server/chrome";
import { getTheme, getUi } from "../../../lib/i18n/server.ts";
import {
  ConsoleBreadcrumb, ConsoleShell,
} from "../../../components/shell/ConsoleShell.tsx";
import { PublicShell } from "../../../components/shell/PublicShell.tsx";
import {
  Field, ModuleCard, Notice, PageHeader, Panel,
} from "../../../components/ui/primitives.tsx";
import { Icon, type IconName } from "../../../components/ui/Icon.tsx";
import { Reveal } from "../../../components/ui/Reveal.tsx";
import type { MessageKey } from "../../../messages/en.ts";

// The modules of the console, in the order the control centre presents them. A wide
// card spans half the row; the narrow ones sit three to a row.
const MODULES: { slug: string; key: string; icon: IconName; wide: boolean }[] = [
  { slug: "agents", key: "agents", icon: "agents", wide: true },
  { slug: "catalog", key: "catalog", icon: "catalog", wide: true },
  { slug: "integrations", key: "integrations", icon: "integrations", wide: true },
  { slug: "runs", key: "runs", icon: "runs", wide: true },
  { slug: "approvals", key: "approvals", icon: "approvals", wide: false },
  { slug: "tools", key: "tools", icon: "tools", wide: false },
  { slug: "knowledge", key: "knowledge", icon: "knowledge", wide: false },
  { slug: "evaluations", key: "evaluations", icon: "evaluations", wide: true },
  { slug: "spend", key: "spend", icon: "spend", wide: true },
];

export default async function WorkspaceHub({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ error?: string; saved?: string }>;
}) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  if (!z.uuid().safeParse(id).success) {
    const [ui, theme] = await Promise.all([getUi(), getTheme()]);
    return <PublicShell ui={ui} theme={theme}>
      <Panel className="auth-panel">
        <h1 className="page-title">{ui.t("workspaces.notFoundTitle")}</h1>
        <p><a className="link" href="/workspaces">{ui.t("workspaces.backToWorkspaces")}</a></p>
      </Panel>
    </PublicShell>;
  }

  let workspace;
  try {
    workspace = await api(session, "/api/v1/workspaces/" + id, workspaceSchema);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) redirect("/login?error=session_expired");
    const chrome = await consoleChrome(session);
    const { ui } = chrome;
    const denied = error instanceof ApiError && [403, 404].includes(error.status);
    return <ConsoleShell chrome={chrome}>
      <h1 className="page-title">
        {ui.t(denied ? "workspaces.deniedTitle" : "workspaces.workspaceUnavailableTitle")}</h1>
      <Notice live="alert" tone="danger">
        {ui.t(denied ? "errors.deniedBody" : "errors.unavailableBody")}</Notice>
      <p><a className="link" href="/workspaces">{ui.t("workspaces.backToWorkspaces")}</a></p>
    </ConsoleShell>;
  }

  const chrome = await consoleChrome(session, workspace);
  const { ui } = chrome;
  const canRename = workspace.role !== "member";
  const canGrantAccess = workspace.role === "owner";

  return <ConsoleShell
    chrome={chrome}
    active="overview"
    breadcrumb={<ConsoleBreadcrumb ui={ui} workspace={workspace}/>}
  >
    <PageHeader
      eyebrow={ui.t("hub.eyebrow")}
      title={ui.t("hub.title")}
      intro={ui.t("hub.intro")}
      actions={<a className="button" href={`/workspaces/${id}/agents`}>
        {ui.t("hub.openAgents")}<Icon name="arrowRight" size={18}/>
      </a>}
    />

    {query.error && <Notice live="alert" tone="danger">
      {ui.t("common.saveFailed")} {ui.t("common.checkInputs")}
    </Notice>}
    {query.saved && <Notice live="status" tone="success">{ui.t("common.saved")}</Notice>}

    <nav className="module-grid" aria-label={ui.t("navigation.primary")}>
      {MODULES.map((module, index) => <ModuleCard
        key={module.slug}
        href={`/workspaces/${id}/${module.slug}`}
        icon={module.icon}
        wide={module.wide}
        title={ui.t(`hub.module.${module.key}.name` as MessageKey)}
        detail={ui.t(`hub.module.${module.key}.detail` as MessageKey)}
        delayMs={Math.min(index * 45, 180)}
      />)}
    </nav>

    <Reveal>
      <Panel labelledBy="workspace-settings-title">
        <h2 id="workspace-settings-title" className="section-title">{ui.t("settings.title")}</h2>
        <p className="notice">{ui.t("settings.intro")}</p>
        {canRename
          ? <form className="form-row" action="/workspaces/mutate" method="post">
            <input type="hidden" name="csrf" value={session.csrf}/>
            <input type="hidden" name="workspace" value={id}/>
            <input type="hidden" name="operation" value="rename"/>
            <Field id="workspace-name" label={ui.t("workspaces.nameLabel")}>
              <input
                className="field-control" id="workspace-name" name="name" required
                maxLength={100} defaultValue={workspace.name} autoComplete="off"
              />
            </Field>
            <button type="submit" className="button">{ui.t("settings.saveName")}</button>
          </form>
          : <Notice tone="warning">{ui.t("settings.memberNotice")}</Notice>}

        <hr className="divider"/>

        <h2 className="section-title">{ui.t("settings.teamTitle")}</h2>
        <p className="notice">{ui.t("settings.teamIntro")}</p>
        {canGrantAccess
          ? <form className="form-row" action="/workspaces/mutate" method="post">
            <input type="hidden" name="csrf" value={session.csrf}/>
            <input type="hidden" name="workspace" value={id}/>
            <input type="hidden" name="operation" value="member"/>
            <Field
              id="subject" label={ui.t("settings.accountId")} help={ui.t("settings.accountIdHelp")}
            >
              <input
                className="field-control" id="subject" name="subject" required maxLength={255}
                autoComplete="off" placeholder={ui.t("settings.accountIdPlaceholder")}
                aria-describedby="subject-help"
              />
            </Field>
            <Field id="role" label={ui.t("settings.roleLabel")}>
              <select className="field-control" id="role" name="role" defaultValue="member">
                <option value="member">{ui.t("settings.roleMember")}</option>
                <option value="admin">{ui.t("settings.roleAdmin")}</option>
              </select>
            </Field>
            <button type="submit" className="button">{ui.t("settings.saveAccess")}</button>
          </form>
          : <Notice tone="warning">
            {ui.t(canRename ? "settings.adminNotice" : "settings.memberNotice")}
          </Notice>}
      </Panel>
    </Reveal>
  </ConsoleShell>;
}
