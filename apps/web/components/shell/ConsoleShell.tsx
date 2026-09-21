import type { Ui } from "../../lib/i18n/messages.ts";
import type { Theme } from "../../lib/i18n/theme.ts";
import type { Workspace } from "../../lib/workspace-contracts";
import { BrandMark, Icon, type IconName } from "../ui/Icon.tsx";
import { Badge } from "../ui/primitives.tsx";
import { LanguageControl } from "./LanguageControl.tsx";
import { MobileNav } from "./MobileNav.tsx";
import { ThemeControl } from "./ThemeControl.tsx";
import type { MessageKey } from "../../messages/en.ts";

export type NavKey =
  | "overview" | "agents" | "runs" | "approvals" | "tools" | "knowledge"
  | "evaluations" | "spend";

const NAV: { key: NavKey; slug: string; icon: IconName; label: MessageKey }[] = [
  { key: "overview", slug: "", icon: "overview", label: "navigation.overview" },
  { key: "agents", slug: "/agents", icon: "agents", label: "navigation.agents" },
  { key: "runs", slug: "/runs", icon: "runs", label: "navigation.runs" },
  { key: "approvals", slug: "/approvals", icon: "approvals", label: "navigation.approvals" },
  { key: "tools", slug: "/tools", icon: "tools", label: "navigation.tools" },
  { key: "knowledge", slug: "/knowledge", icon: "knowledge", label: "navigation.knowledge" },
  { key: "evaluations", slug: "/evaluations", icon: "evaluations", label: "navigation.evaluations" },
  { key: "spend", slug: "/spend", icon: "spend", label: "navigation.spend" },
];

export interface ConsoleChrome {
  ui: Ui;
  theme: Theme;
  csrf: string;
  workspace: Workspace | null;
  /** null means the list could not be read: the switcher then shows no invented choices. */
  workspaces: Workspace[] | null;
}

function Brand({ ui }: { ui: Ui }) {
  return <a className="brand" href="/workspaces">
    <span className="brand-mark"><BrandMark size={19}/></span>
    <span>
      <span className="brand-name">{ui.t("common.brand")}</span><br/>
      <span className="brand-suffix">{ui.t("common.brandSuffix")}</span>
    </span>
  </a>;
}

function WorkspaceSwitcher({ ui, workspace, workspaces }: {
  ui: Ui; workspace: Workspace; workspaces: Workspace[] | null;
}) {
  const others = (workspaces ?? []).filter(item => item.id !== workspace.id);
  return <div className="sidebar-section">
    <p className="sidebar-label" id="workspace-switcher-label">{ui.t("navigation.workspaceLabel")}</p>
    {workspaces === null || others.length === 0
      ? <>
        <p className="nav-item" aria-current="page"><Icon name="workspaces" size={18}/>
          <span>{workspace.name}</span></p>
        <a className="nav-item" href="/workspaces">
          <Icon name="chevronRight" size={18}/><span>{ui.t("workspaces.allWorkspaces")}</span></a>
        {workspaces === null && <p className="field-help">
          {ui.t("navigation.workspaceListUnavailable")}</p>}
      </>
      : <details className="workspace-switcher">
        <summary aria-describedby="workspace-switcher-label">
          <Icon name="workspaces" size={18}/>
          <span>{workspace.name}</span>
          <Icon name="chevronDown" size={18}/>
        </summary>
        <div className="workspace-switcher-list">
          {others.map(item => <a key={item.id} className="nav-item" href={`/workspaces/${item.id}`}>
            <span>{item.name}</span>
          </a>)}
          <a className="nav-item" href="/workspaces">
            <Icon name="workspaces" size={18}/><span>{ui.t("workspaces.allWorkspaces")}</span>
          </a>
        </div>
      </details>}
  </div>;
}

function NavContent({ chrome, active }: { chrome: ConsoleChrome; active?: NavKey }) {
  const { ui, workspace, workspaces, theme, csrf } = chrome;
  return <>
    {workspace && <WorkspaceSwitcher ui={ui} workspace={workspace} workspaces={workspaces}/>}
    {workspace && <nav className="sidebar-nav" aria-label={ui.t("navigation.primary")}>
      {NAV.map(item => <a
        key={item.key}
        className="nav-item"
        href={`/workspaces/${workspace.id}${item.slug}`}
        aria-current={active === item.key ? "page" : undefined}
      ><Icon name={item.icon} size={20}/><span>{ui.t(item.label)}</span></a>)}
    </nav>}
    <div className="sidebar-foot">
      <ThemeControl
        label={ui.t("theme.label")}
        current={theme}
        labels={{
          system: ui.t("theme.system"), light: ui.t("theme.light"), dark: ui.t("theme.dark"),
        }}
      />
      <LanguageControl label={ui.t("language.label")} current={ui.locale}/>
      <a className="nav-item" href="/workspaces">
        <Icon name="workspaces" size={20}/><span>{ui.t("navigation.workspaces")}</span>
      </a>
      <form method="post" action="/auth/logout">
        <input type="hidden" name="csrf" value={csrf}/>
        <button type="submit" className="nav-item">
          <Icon name="signOut" size={20}/><span>{ui.t("navigation.signOut")}</span>
        </button>
      </form>
    </div>
  </>;
}

function initials(name: string): string {
  return name.split(/\s+/).filter(Boolean).slice(0, 2)
    .map(word => word.slice(0, 1).toLocaleUpperCase("tr-TR")).join("") || "•";
}

/**
 * The signed-in application frame. The same navigation tree is rendered once for the
 * persistent sidebar and once inside the narrow-screen drawer, so no module can be
 * present on one and missing on the other.
 */
export function ConsoleShell({ chrome, active, breadcrumb, children }: {
  chrome: ConsoleChrome;
  active?: NavKey;
  breadcrumb?: React.ReactNode;
  children: React.ReactNode;
}) {
  const { ui, workspace } = chrome;
  return <div className="shell">
    <a className="skip-link" href="#main-content">{ui.t("navigation.skipToContent")}</a>
    <aside className="sidebar">
      <Brand ui={ui}/>
      <NavContent chrome={chrome} active={active}/>
    </aside>
    <div className="main">
      <div className="mobile-bar">
        <MobileNav
          openLabel={ui.t("navigation.openMenu")}
          closeLabel={ui.t("navigation.closeMenu")}
          title={ui.t("navigation.workspaceMenu")}
        >
          <Brand ui={ui}/>
          <NavContent chrome={chrome} active={active}/>
        </MobileNav>
        <Brand ui={ui}/>
        <span className="workspace-initials" aria-hidden="true">
          {workspace ? initials(workspace.name) : initials(ui.t("common.brand"))}
        </span>
      </div>
      <main id="main-content" className="content route-enter">
        {breadcrumb}
        {children}
      </main>
    </div>
  </div>;
}

/** Workspace trail plus the reader's own role in it, which is never invented. */
export function ConsoleBreadcrumb({ ui, workspace, trail }: {
  ui: Ui; workspace?: Workspace | null; trail?: { href: string; label: string }[];
}) {
  return <nav className="breadcrumb" aria-label={ui.t("navigation.breadcrumb")}>
    <span className="breadcrumb-trail">
      <a className="link" href="/workspaces">{ui.t("navigation.workspaces")}</a>
      {workspace && <>
        <span className="breadcrumb-sep" aria-hidden="true">/</span>
        <a className="link" href={`/workspaces/${workspace.id}`}>{workspace.name}</a>
      </>}
      {trail?.map(step => <span key={step.href} className="breadcrumb-trail">
        <span className="breadcrumb-sep" aria-hidden="true">/</span>
        <a className="link" href={step.href}>{step.label}</a>
      </span>)}
    </span>
    {workspace && <Badge accent>
      {ui.t("workspaces.yourRole", { role: ui.t(`workspaces.role.${workspace.role}`) })}
    </Badge>}
  </nav>;
}
