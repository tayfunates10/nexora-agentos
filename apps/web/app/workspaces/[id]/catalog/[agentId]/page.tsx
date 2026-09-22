import { randomUUID } from "node:crypto";
import { redirect } from "next/navigation";
import { z } from "zod";
import { api } from "../../../../../lib/server/api";
import { currentSession } from "../../../../../lib/server/session";
import { consoleChrome } from "../../../../../lib/server/chrome";
import {
  INSTANCE_STATUS_TONES, instanceStatusKey, tenantAgentSchema, unmetRequirements,
  updateAvailable, versionEventPageSchema, type Requirement, type TenantAgent,
} from "../../../../../lib/catalog-contracts.ts";
import {
  integrationPageSchema, type TenantIntegration,
} from "../../../../../lib/integration-contracts.ts";
import { workspaceSchema } from "../../../../../lib/workspace-contracts";
import { formatTimestamp } from "../../../../../lib/i18n/format.ts";
import type { Ui } from "../../../../../lib/i18n/messages.ts";
import { ConsoleBreadcrumb, ConsoleShell } from "../../../../../components/shell/ConsoleShell.tsx";
import {
  Badge, Detail, DetailList, Disclosure, Field, Hint, Notice, PageHeader, Panel, StatusBadge,
} from "../../../../../components/ui/primitives.tsx";
import { Reveal } from "../../../../../components/ui/Reveal.tsx";
import { ConsoleProblem, Failed, InvalidLinkPage, Saved } from "../../console";
import type { MessageKey } from "../../../../../messages/en.ts";

const MESSAGES: Record<string, MessageKey> = {
  invalid: "catalog.error.invalid",
  forbidden: "catalog.error.forbidden",
  conflict: "catalog.error.conflict",
  failed: "catalog.error.failed",
};

/**
 * One integration the agent declares, and which of the workspace's own accounts fills it.
 * The choice is between connected accounts; a credential is never part of this form.
 */
function BindingRow({ requirement, accounts, agent, workspaceId, csrf, canManage, ui }: {
  requirement: Requirement;
  accounts: TenantIntegration[];
  agent: TenantAgent;
  workspaceId: string; csrf: string; canManage: boolean; ui: Ui;
}) {
  const service = requirement.integration_definition_id;
  const usable = accounts.filter(item => item.integration_definition_id === service);
  const fieldId = `bind-${agent.id}-${service}`;
  return <article className="card">
    <Badge accent={requirement.required}>
      {ui.t(requirement.required ? "catalog.required" : "catalog.optional")}
    </Badge>
    <h3 className="section-title">{service}</h3>
    <p className="field-help">
      {requirement.bound_display_name ?? ui.t("catalog.notConnected")}
    </p>
    {canManage && (usable.length > 0
      ? <form className="form" action="/workspaces/tenant-agents/mutate" method="post">
        <input type="hidden" name="csrf" value={csrf}/>
        <input type="hidden" name="workspace" value={workspaceId}/>
        <input type="hidden" name="agent" value={agent.id}/>
        <input type="hidden" name="operation" value="bind"/>
        <input type="hidden" name="binding" value={service}/>
        <Field id={fieldId} label={ui.t("catalog.bindLabel", { service })}>
          <select
            className="field-control" id={fieldId} name="integration"
            defaultValue={requirement.bound_integration_id ?? ""}
          >
            {usable.map(item => <option key={item.id} value={item.id}>
              {item.display_name} — {item.account_identifier}
            </option>)}
          </select>
        </Field>
        <div className="button-row">
          <button type="submit" className="button">{ui.t("catalog.bindSubmit")}</button>
        </div>
      </form>
      : <Notice tone="warning">
        {ui.t("catalog.noAccountsFor", { service })}{" "}
        <a href={`/workspaces/${workspaceId}/integrations`}>
          {ui.t("catalog.connectFirst", { service })}
        </a>
      </Notice>)}
    {canManage && requirement.bound_integration_id && <form
      className="form" action="/workspaces/tenant-agents/mutate" method="post"
    >
      <input type="hidden" name="csrf" value={csrf}/>
      <input type="hidden" name="workspace" value={workspaceId}/>
      <input type="hidden" name="agent" value={agent.id}/>
      <input type="hidden" name="operation" value="unbind"/>
      <input type="hidden" name="binding" value={service}/>
      <button type="submit" className="button secondary small">{ui.t("catalog.unbind")}</button>
    </form>}
  </article>;
}

export default async function AgentInstance({ params, searchParams }: {
  params: Promise<{ id: string; agentId: string }>;
  searchParams: Promise<{ saved?: string; error?: string }>;
}) {
  const [{ id, agentId }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const root = `/workspaces/${id}/catalog`;
  let chrome = await consoleChrome(session);
  if (!z.uuid().safeParse(id).success || !z.uuid().safeParse(agentId).success) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: root, label: chrome.ui.t("navigation.catalog"),
    }}/>;
  }

  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
    const instance = `/api/v1/workspaces/${id}/tenant-agents/${agentId}`;
    const [agent, history, connections] = await Promise.all([
      api(session, instance, tenantAgentSchema),
      api(session, `${instance}/history?limit=25`, versionEventPageSchema),
      api(session, `/api/v1/workspaces/${id}/integrations?limit=100`, integrationPageSchema),
    ]);
    const canManage = workspace.role !== "member";
    const missing = unmetRequirements(agent);
    const hidden = { csrf: session.csrf, workspace: id, agent: agent.id };

    return <ConsoleShell
      chrome={chrome}
      active="catalog"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace}
        trail={[
          { href: root, label: ui.t("navigation.catalog") },
          { href: `${root}/${agent.id}`, label: agent.display_name },
        ]}
      />}
    >
      <PageHeader
        eyebrow={agent.slug}
        title={agent.display_name}
        intro={agent.manifest.description}
      />
      {/* Installing lands here, so this page confirms the add rather than a generic save. */}
      <Saved message={query.saved === "forked"
        ? ui.t("catalog.forked")
        : query.saved === "added"
          ? ui.t("catalog.added")
          : query.saved ? ui.t("catalog.saved") : null}/>
      <Failed message={query.error ? ui.t(MESSAGES[query.error] ?? MESSAGES.failed) : null}/>

      <Panel labelledBy="status-title">
        <h2 id="status-title" className="section-title">{ui.t("catalog.readyTitle")}</h2>
        <StatusBadge tone={INSTANCE_STATUS_TONES[agent.status]}>
          {ui.t(instanceStatusKey(agent.status))}
        </StatusBadge>
        <p className="notice">
          {ui.t(`catalog.instanceStatusHelp.${agent.status}` as MessageKey)}
        </p>
        <Notice tone={agent.readiness.ready ? "success" : "warning"}>
          {agent.readiness.ready
            ? ui.t("catalog.ready")
            : ui.t("catalog.notReady", { count: String(missing.length) })}
        </Notice>

        <div className="card-list">{agent.readiness.requirements.map(requirement =>
          <BindingRow
            key={requirement.integration_definition_id}
            requirement={requirement}
            accounts={connections.items}
            agent={agent}
            workspaceId={id}
            csrf={session.csrf}
            canManage={canManage}
            ui={ui}
          />)}</div>

        {canManage && <div className="button-row">
          <form action="/workspaces/tenant-agents/mutate" method="post">
            {Object.entries({
              ...hidden,
              operation: "status",
              status: agent.status === "active" ? "paused" : "active",
            }).map(([name, value]) =>
              <input key={name} type="hidden" name={name} value={value}/>)}
            <button type="submit" className="button secondary">
              {ui.t(agent.status === "active" ? "catalog.pause" : "catalog.activate")}
            </button>
          </form>
          <form action="/workspaces/tenant-agents/mutate" method="post">
            {Object.entries({ ...hidden, operation: "remove" }).map(([name, value]) =>
              <input key={name} type="hidden" name={name} value={value}/>)}
            <button type="submit" className="button danger">{ui.t("catalog.remove")}</button>
          </form>
        </div>}
      </Panel>

      <Panel labelledBy="run-standard-agent-title">
        <h2 id="run-standard-agent-title" className="section-title">
          {ui.t("agents.startWith", { agentName: agent.display_name })}
        </h2>
        {agent.status === "active" && agent.readiness.ready
          ? <form className="form" action="/workspaces/runs/start" method="post">
            <input type="hidden" name="csrf" value={session.csrf}/>
            <input type="hidden" name="workspace" value={id}/>
            <input type="hidden" name="agent" value={agent.id}/>
            <input type="hidden" name="kind" value="standard"/>
            <input type="hidden" name="idempotency" value={randomUUID()}/>
            <Field
              id={`task-${agent.id}`}
              label={ui.t("agents.taskLabel")}
              help={ui.t("agents.taskHelp")}
            >
              <textarea
                className="field-control"
                id={`task-${agent.id}`}
                name="input"
                required
                rows={5}
                maxLength={20000}
                aria-describedby={`task-${agent.id}-help`}
              />
            </Field>
            <div><button type="submit" className="button">{ui.t("agents.startRun")}</button></div>
          </form>
          : <Notice tone="warning">
            {agent.readiness.ready
              ? ui.t(`catalog.instanceStatusHelp.${agent.status}` as MessageKey)
              : ui.t("catalog.notReady", { count: String(missing.length) })}
          </Notice>}
      </Panel>

      <Reveal>
        <Panel labelledBy="version-title">
          <h2 id="version-title" className="section-title">{ui.t("catalog.updateTitle")}</h2>
          <DetailList>
            <Detail label={ui.t("catalog.installedVersion")}>{agent.version}</Detail>
            <Detail label={ui.t("catalog.latestVersion")}>
              {agent.available_version ?? ui.t("common.empty")}
            </Detail>
            <Detail label={ui.t("catalog.channelLabel")}>
              {ui.t(`catalog.channel.${agent.update_channel}` as MessageKey)}
            </Detail>
            <Detail label={ui.t("catalog.modeLabel")}>
              {ui.t(`catalog.mode.${agent.update_mode}` as MessageKey)}
            </Detail>
          </DetailList>
          {agent.manifest.changelog && <>
            <h3 className="section-title">{ui.t("catalog.changes")}</h3>
            {/* Published release notes, rendered as the text they were written as. */}
            <p className="notice" style={{ whiteSpace: "pre-wrap" }}>{agent.manifest.changelog}</p>
          </>}
          {!updateAvailable(agent) && <Hint>{ui.t("catalog.upToDate")}</Hint>}
          {canManage && <div className="button-row">
            {updateAvailable(agent) && <form
              action="/workspaces/tenant-agents/mutate" method="post"
            >
              {Object.entries({ ...hidden, operation: "update" }).map(([name, value]) =>
                <input key={name} type="hidden" name={name} value={value}/>)}
              <button type="submit" className="button">
                {ui.t("catalog.update")} → {agent.available_version}
              </button>
            </form>}
            <form action="/workspaces/tenant-agents/mutate" method="post">
              {Object.entries({ ...hidden, operation: "rollback" }).map(([name, value]) =>
                <input key={name} type="hidden" name={name} value={value}/>)}
              <button type="submit" className="button secondary">{ui.t("catalog.rollback")}</button>
            </form>
          </div>}
          {canManage && <Hint>{ui.t("catalog.rollbackHelp")}</Hint>}

          <h3 className="section-title">{ui.t("catalog.historyTitle")}</h3>
          <DetailList>{history.items.map(event => <Detail
            key={event.id}
            label={ui.t(`catalog.historyAction.${event.action}` as MessageKey)}
          >
            {event.from_version
              ? ui.t("catalog.historyLine", { from: event.from_version, to: event.to_version })
              : event.to_version}
            {" · "}
            <time dateTime={event.created_at}>
              {formatTimestamp(event.created_at, ui.locale)}
            </time>
          </Detail>)}</DetailList>
        </Panel>
      </Reveal>

      <Reveal>
        <Panel labelledBy="settings-title">
          <h2 id="settings-title" className="section-title">{ui.t("catalog.settingsTitle")}</h2>
          {canManage
            ? <form className="form" action="/workspaces/tenant-agents/mutate" method="post">
              {Object.entries({ ...hidden, operation: "settings" }).map(([name, value]) =>
                <input key={name} type="hidden" name={name} value={value}/>)}
              <Field id="display_name" label={ui.t("catalog.displayNameLabel")}>
                <input
                  className="field-control" id="display_name" name="display_name" required
                  maxLength={100} autoComplete="off" defaultValue={agent.display_name}
                />
              </Field>
              <Field
                id="instructions"
                label={ui.t("catalog.instructionsLabel")}
                help={ui.t("catalog.instructionsHelp")}
              >
                <textarea
                  className="field-control" id="instructions" name="instructions_override"
                  rows={5} maxLength={20000} aria-describedby="instructions-help"
                  defaultValue={agent.instructions_override ?? ""}
                />
              </Field>
              <Field
                id="settings"
                label={ui.t("catalog.settingsLabel")}
                help={ui.t("catalog.settingsHelp")}
              >
                <textarea
                  className="field-control" id="settings" name="settings" rows={6}
                  maxLength={20000} aria-describedby="settings-help"
                  defaultValue={JSON.stringify(agent.settings, null, 2)}
                />
              </Field>
              <div><button type="submit" className="button">{ui.t("common.save")}</button></div>
            </form>
            : <Hint>{ui.t("catalog.readOnlyNotice")}</Hint>}
        </Panel>
      </Reveal>

      {canManage && <Reveal>
        <Panel labelledBy="fork-title">
          <h2 id="fork-title" className="section-title">{ui.t("catalog.forkTitle")}</h2>
          <p className="notice">{ui.t("catalog.forkHelp")}</p>
          <Disclosure summary={ui.t("catalog.forkSubmit")}>
            <form className="form" action="/workspaces/tenant-agents/mutate" method="post">
              {Object.entries({ ...hidden, operation: "fork" }).map(([name, value]) =>
                <input key={name} type="hidden" name={name} value={value}/>)}
              <Field id="fork_name" label={ui.t("catalog.forkNameLabel")}>
                <input
                  className="field-control" id="fork_name" name="name" required maxLength={100}
                  autoComplete="off" defaultValue={agent.display_name}
                />
              </Field>
              <div>
                <button type="submit" className="button">{ui.t("catalog.forkSubmit")}</button>
              </div>
            </form>
          </Disclosure>
        </Panel>
      </Reveal>}
    </ConsoleShell>;
  } catch (error) {
    return <ConsoleProblem
      chrome={chrome} error={error} area={chrome.ui.t("catalog.area")} active="catalog"
      back={{ href: root, label: chrome.ui.t("navigation.catalog") }}
    />;
  }
}
