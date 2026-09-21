import { redirect } from "next/navigation";
import { z } from "zod";
import { currentSession } from "../../lib/server/session";
import { api, ApiError } from "../../lib/server/api";
import { pageSchema } from "../../lib/workspace-contracts";
import { consoleChrome } from "../../lib/server/chrome";
import { ConsoleShell } from "../../components/shell/ConsoleShell.tsx";
import {
  Badge, EmptyState, Field, Notice, PageHeader, Pagination, Panel,
} from "../../components/ui/primitives.tsx";
import { Icon } from "../../components/ui/Icon.tsx";
import { Reveal } from "../../components/ui/Reveal.tsx";

export default async function Workspaces({ searchParams }: {
  searchParams: Promise<{ cursor?: string; error?: string; saved?: string }>;
}) {
  const session = await currentSession();
  if (!session) redirect("/login");
  const query = await searchParams;
  const chrome = await consoleChrome(session);
  const { ui } = chrome;
  const cursor = z.uuid().safeParse(query.cursor);

  if (query.cursor && !cursor.success) {
    return <ConsoleShell chrome={chrome}>
      <h1 className="page-title">{ui.t("workspaces.invalidPageTitle")}</h1>
      <Notice live="alert" tone="danger">{ui.t("common.invalidLinkBody")}</Notice>
      <p><a className="link" href="/workspaces">{ui.t("workspaces.backToWorkspaces")}</a></p>
    </ConsoleShell>;
  }

  let page;
  try {
    page = await api(
      session, "/api/v1/workspaces" + (cursor.success ? "?cursor=" + cursor.data : ""), pageSchema,
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) redirect("/login?error=session_expired");
    return <ConsoleShell chrome={chrome}>
      <h1 className="page-title">{ui.t("workspaces.unavailableTitle")}</h1>
      <Notice live="alert" tone="danger">{ui.t("workspaces.unavailableBody")}</Notice>
      <p><a className="link" href="/workspaces">{ui.t("common.retry")}</a></p>
    </ConsoleShell>;
  }

  return <ConsoleShell chrome={chrome}>
    <PageHeader
      eyebrow={ui.t("workspaces.eyebrow")}
      title={ui.t("workspaces.title")}
      intro={ui.t("workspaces.intro")}
    />
    {query.error && <Notice live="alert" tone="danger">
      {ui.t("common.saveFailed")} {ui.t("common.checkInputs")}
    </Notice>}
    {query.saved && <Notice live="status" tone="success">{ui.t("common.saved")}</Notice>}

    {page.items.length === 0
      ? <EmptyState title={ui.t("workspaces.emptyTitle")}>
        <p>{ui.t("workspaces.emptyBody")}</p>
      </EmptyState>
      : <div className="card-grid">{page.items.map(workspace => <a
        className="card card-link" key={workspace.id} href={"/workspaces/" + workspace.id}
      >
        <Badge>{ui.t(`workspaces.role.${workspace.role}`)}</Badge>
        <h2 className="section-title">{workspace.name}</h2>
        <p className="notice">{ui.t("workspaces.manage")}</p>
      </a>)}</div>}

    <Pagination
      label={ui.t("workspaces.pagesLabel")}
      previous={query.cursor ? { href: "/workspaces", label: ui.t("common.firstPage") } : null}
      next={page.next_cursor
        ? { href: "/workspaces?cursor=" + page.next_cursor, label: ui.t("common.nextPage") }
        : null}
    />

    <Reveal>
      <Panel labelledBy="create-workspace-title">
        <h2 id="create-workspace-title" className="section-title">{ui.t("workspaces.createTitle")}</h2>
        <form className="form" action="/workspaces/mutate" method="post">
          <input type="hidden" name="csrf" value={session.csrf}/>
          <input type="hidden" name="operation" value="create"/>
          <Field id="name" label={ui.t("workspaces.nameLabel")}>
            <input
              className="field-control" id="name" name="name" required maxLength={100}
              placeholder={ui.t("workspaces.namePlaceholder")} autoComplete="off"
            />
          </Field>
          <div>
            <button type="submit" className="button">
              {ui.t("workspaces.createSubmit")}<Icon name="arrowRight" size={18}/>
            </button>
          </div>
        </form>
      </Panel>
    </Reveal>
  </ConsoleShell>;
}
