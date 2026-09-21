import { randomUUID } from "node:crypto";
import { redirect } from "next/navigation";
import { z } from "zod";
import { agentPageSchema } from "../../../../lib/agent-contracts";
import { api } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { consoleChrome } from "../../../../lib/server/chrome";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { formatTimestamp } from "../../../../lib/i18n/format.ts";
import { ConsoleBreadcrumb, ConsoleShell } from "../../../../components/shell/ConsoleShell.tsx";
import {
  Badge, Disclosure, EmptyState, Field, Hint, Notice, PageHeader, Pagination, Panel,
} from "../../../../components/ui/primitives.tsx";
import { Reveal } from "../../../../components/ui/Reveal.tsx";
import { ConsoleProblem, Failed, InvalidLinkPage, Saved } from "../console";
import type { MessageKey } from "../../../../messages/en.ts";

const MESSAGES: Record<string, MessageKey> = {
  invalid: "agents.error.invalid",
  forbidden: "agents.error.forbidden",
  failed: "agents.error.failed",
};

export default async function Agents({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string; saved?: string; error?: string }>;
}) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const root = `/workspaces/${id}/agents`;
  let chrome = await consoleChrome(session);
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !z.uuid().safeParse(query.cursor).success)) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: "/workspaces", label: chrome.ui.t("workspaces.backToWorkspaces"),
    }}/>;
  }

  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
    const agents = await api(
      session,
      `/api/v1/workspaces/${id}/agents?limit=25${query.cursor ? "&cursor=" + query.cursor : ""}`,
      agentPageSchema,
    );
    const canManage = workspace.role !== "member";

    return <ConsoleShell
      chrome={chrome}
      active="agents"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace}
        trail={[{ href: root, label: ui.t("navigation.agents") }]}
      />}
    >
      <PageHeader
        eyebrow={ui.t("agents.eyebrow")}
        title={ui.t("agents.title")}
        intro={ui.t("agents.intro")}
      />
      <Notice tone="warning">{ui.t("agents.workerNotice")}</Notice>
      <Saved message={query.saved === "agent" ? ui.t("agents.created") : null}/>
      <Failed message={query.error ? ui.t(MESSAGES[query.error] ?? MESSAGES.failed) : null}/>

      {agents.items.length === 0
        ? <EmptyState title={ui.t("agents.emptyTitle")}>
          <p>{ui.t(canManage ? "agents.emptyManage" : "agents.emptyMember")}</p>
        </EmptyState>
        : <div className="card-list">{agents.items.map(agent => <article
          className="card" key={agent.id}
        >
          <Badge>{ui.t("agents.profile", { profile: agent.model_profile })}</Badge>
          <h2 className="section-title">{agent.name}</h2>
          {/* The instruction text is what someone wrote; it is shown as stored. */}
          <p className="notice" style={{ whiteSpace: "pre-wrap" }}>{agent.instructions}</p>
          <p className="field-help">
            <time dateTime={agent.created_at}>{formatTimestamp(agent.created_at, ui.locale)}</time>
          </p>
          <Disclosure summary={ui.t("agents.startWith", { agentName: agent.name })}>
            <form className="form" action="/workspaces/runs/start" method="post">
              <input type="hidden" name="csrf" value={session.csrf}/>
              <input type="hidden" name="workspace" value={id}/>
              <input type="hidden" name="agent" value={agent.id}/>
              {/* Minted per render: submitting the same form twice replays one idempotency
                  key, so a double click returns the existing run instead of starting another. */}
              <input type="hidden" name="idempotency" value={randomUUID()}/>
              <Field
                id={`input-${agent.id}`}
                label={ui.t("agents.taskLabel")}
                help={ui.t("agents.taskHelp")}
              >
                <textarea
                  className="field-control" id={`input-${agent.id}`} name="input" required
                  rows={4} maxLength={20000} aria-describedby={`input-${agent.id}-help`}
                />
              </Field>
              <div><button type="submit" className="button">{ui.t("agents.startRun")}</button></div>
            </form>
          </Disclosure>
        </article>)}</div>}

      <Pagination
        label={ui.t("agents.pagesLabel")}
        previous={query.cursor ? { href: root, label: ui.t("common.firstPage") } : null}
        next={agents.next_cursor
          ? { href: `${root}?cursor=${agents.next_cursor}`, label: ui.t("agents.more") }
          : null}
      />

      <Reveal>
        <Panel labelledBy="create-agent-title">
          <h2 id="create-agent-title" className="section-title">{ui.t("agents.createTitle")}</h2>
          {canManage
            ? <form className="form" action="/workspaces/agents/create" method="post">
              <input type="hidden" name="csrf" value={session.csrf}/>
              <input type="hidden" name="workspace" value={id}/>
              <Field id="name" label={ui.t("agents.nameLabel")}>
                <input
                  className="field-control" id="name" name="name" required maxLength={100}
                  autoComplete="off"
                />
              </Field>
              <Field id="instructions" label={ui.t("agents.instructionsLabel")}>
                <textarea
                  className="field-control" id="instructions" name="instructions" required
                  rows={6} maxLength={20000}
                />
              </Field>
              <Field
                id="model_profile"
                label={ui.t("agents.profileLabel")}
                help={ui.t("agents.profileHelp")}
              >
                <input
                  className="field-control" id="model_profile" name="model_profile" required
                  maxLength={64} defaultValue="default" pattern="[A-Za-z0-9._\-]+"
                  autoComplete="off" aria-describedby="model_profile-help"
                />
              </Field>
              <div>
                <button type="submit" className="button">{ui.t("agents.createSubmit")}</button>
              </div>
            </form>
            : <Hint>{ui.t("agents.readOnlyNotice")}</Hint>}
        </Panel>
      </Reveal>
    </ConsoleShell>;
  } catch (error) {
    return <ConsoleProblem
      chrome={chrome} error={error} area={chrome.ui.t("agents.area")} active="agents"
      back={{ href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace") }}
    />;
  }
}
