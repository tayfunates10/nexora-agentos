import { redirect } from "next/navigation";
import { z } from "zod";
import { api } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { consoleChrome } from "../../../../lib/server/chrome";
import {
  INSTANCE_STATUS_TONES, catalogPageSchema, instanceStatusKey, tenantAgentPageSchema,
  updateAvailable, updatePolicySchema,
} from "../../../../lib/catalog-contracts.ts";
import { integrationPageSchema } from "../../../../lib/integration-contracts.ts";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { ConsoleBreadcrumb, ConsoleShell } from "../../../../components/shell/ConsoleShell.tsx";
import {
  Badge, EmptyState, Field, Hint, Notice, PageHeader, Panel, StatusBadge,
} from "../../../../components/ui/primitives.tsx";
import { Reveal } from "../../../../components/ui/Reveal.tsx";
import { ConsoleProblem, Failed, InvalidLinkPage, Saved } from "../console";
import type { MessageKey } from "../../../../messages/en.ts";

const MESSAGES: Record<string, MessageKey> = {
  invalid: "catalog.error.invalid",
  forbidden: "catalog.error.forbidden",
  conflict: "catalog.error.conflict",
  failed: "catalog.error.failed",
};
const CHANNELS = ["stable", "beta", "canary"] as const;
const MODES = ["manual", "automatic"] as const;

export default async function Catalog({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ saved?: string; error?: string }>;
}) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const root = `/workspaces/${id}/catalog`;
  let chrome = await consoleChrome(session);
  if (!z.uuid().safeParse(id).success) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: "/workspaces", label: chrome.ui.t("workspaces.backToWorkspaces"),
    }}/>;
  }

  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
    const [catalog, installed, policy, connections] = await Promise.all([
      api(session, `/api/v1/workspaces/${id}/agent-catalog?limit=50`, catalogPageSchema),
      api(session, `/api/v1/workspaces/${id}/tenant-agents?limit=50`, tenantAgentPageSchema),
      api(session, `/api/v1/workspaces/${id}/agent-update-policy`, updatePolicySchema),
      api(session, `/api/v1/workspaces/${id}/integrations?limit=100`, integrationPageSchema),
    ]);
    const canManage = workspace.role !== "member";
    // Which services this workspace has connected at all, so a card can say what is
    // still missing before the agent is added rather than after.
    const connected = new Set(
      connections.items
        .filter(item => item.status === "connected")
        .map(item => item.integration_definition_id),
    );

    return <ConsoleShell
      chrome={chrome}
      active="catalog"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace}
        trail={[{ href: root, label: ui.t("navigation.catalog") }]}
      />}
    >
      <PageHeader
        eyebrow={ui.t("catalog.eyebrow")}
        title={ui.t("catalog.title")}
        intro={ui.t("catalog.intro")}
      />
      <Saved message={query.saved === "added"
        ? ui.t("catalog.added")
        : query.saved ? ui.t("catalog.saved") : null}/>
      <Failed message={query.error ? ui.t(MESSAGES[query.error] ?? MESSAGES.failed) : null}/>

      <Panel labelledBy="installed-title">
        <h2 id="installed-title" className="section-title">{ui.t("catalog.installedTitle")}</h2>
        {installed.items.length === 0
          ? <Hint>{ui.t("catalog.installedEmpty")}</Hint>
          : <div className="card-list">{installed.items.map(agent => <article
            className="card" key={agent.id}
          >
            <StatusBadge tone={INSTANCE_STATUS_TONES[agent.status]}>
              {ui.t(instanceStatusKey(agent.status))}
            </StatusBadge>
            <h3 className="section-title">{agent.display_name}</h3>
            <p className="field-help">{ui.t("catalog.version", { version: agent.version })}</p>
            {updateAvailable(agent) && <Notice tone="warning">
              {ui.t("catalog.availableVersion", { version: agent.available_version ?? "" })}
            </Notice>}
            {!agent.ready && <Notice tone="warning">
              {ui.t("catalog.instanceStatusHelp.paused")}
            </Notice>}
            <p><a className="card-link" href={`${root}/${agent.id}`}>
              {ui.t("catalog.open")}
            </a></p>
          </article>)}</div>}
      </Panel>

      <Reveal>
        <Panel labelledBy="available-title">
          <h2 id="available-title" className="section-title">{ui.t("catalog.availableTitle")}</h2>
          {catalog.items.length === 0
            ? <EmptyState title={ui.t("catalog.emptyTitle")}>
              <p>{ui.t("catalog.emptyBody")}</p>
            </EmptyState>
            : <div className="card-list">{catalog.items.map(entry => {
              const missing = entry.required_integrations.filter(name => !connected.has(name));
              return <article className="card" key={entry.slug}>
                <Badge>{entry.category}</Badge>
                {entry.installed_count > 0 && <Badge accent>
                  {ui.t("catalog.installedCount", { count: String(entry.installed_count) })}
                </Badge>}
                <h3 className="section-title">{entry.name}</h3>
                <p className="notice">{entry.description}</p>
                <p className="field-help">
                  {entry.available_version
                    ? ui.t("catalog.version", { version: entry.available_version })
                    : ui.t("common.empty")}
                </p>
                {entry.required_integrations.length > 0 && <p className="field-help">
                  {ui.t("catalog.requires")}: {entry.required_integrations.join(", ")}
                </p>}
                {entry.optional_integrations.length > 0 && <p className="field-help">
                  {ui.t("catalog.optionalUses")}: {entry.optional_integrations.join(", ")}
                </p>}
                {entry.capabilities.length > 0 && <p className="field-help">
                  {ui.t("catalog.capabilities")}: {entry.capabilities.join(", ")}
                </p>}
                <p className="field-help">
                  {entry.approval_required.length > 0
                    ? `${ui.t("catalog.approvals")}: ${entry.approval_required.join(", ")}`
                    : ui.t("catalog.noApprovals")}
                </p>

                {/* Missing connections are named before the agent is added, so nobody
                    installs something that cannot run. */}
                {missing.length > 0 && <Notice tone="warning">
                  {missing.map(name => ui.t("catalog.connectFirst", { service: name })).join(" · ")}
                  {" "}
                  <a href={`/workspaces/${id}/integrations`}>{ui.t("navigation.integrations")}</a>
                </Notice>}

                {canManage && entry.available_version && <form
                  className="form" action="/workspaces/tenant-agents/mutate" method="post"
                >
                  <input type="hidden" name="csrf" value={session.csrf}/>
                  <input type="hidden" name="workspace" value={id}/>
                  <input type="hidden" name="operation" value="install"/>
                  <input type="hidden" name="slug" value={entry.slug}/>
                  <Field id={`install-${entry.slug}`} label={ui.t("catalog.displayNameLabel")}>
                    <input
                      className="field-control" id={`install-${entry.slug}`} name="display_name"
                      maxLength={100} autoComplete="off" defaultValue={entry.name}
                    />
                  </Field>
                  <div>
                    <button type="submit" className="button">{ui.t("catalog.add")}</button>
                  </div>
                </form>}
              </article>;
            })}</div>}
          {!canManage && <Hint>{ui.t("catalog.readOnlyNotice")}</Hint>}
        </Panel>
      </Reveal>

      <Reveal>
        <Panel labelledBy="policy-title">
          <h2 id="policy-title" className="section-title">{ui.t("catalog.policyTitle")}</h2>
          <p className="notice">{ui.t("catalog.policyHelp")}</p>
          {canManage
            ? <form className="form" action="/workspaces/tenant-agents/mutate" method="post">
              <input type="hidden" name="csrf" value={session.csrf}/>
              <input type="hidden" name="workspace" value={id}/>
              <input type="hidden" name="operation" value="policy"/>
              <Field
                id="channel"
                label={ui.t("catalog.channelLabel")}
                help={ui.t(`catalog.channelHelp.${policy.channel}` as MessageKey)}
              >
                <select
                  className="field-control" id="channel" name="channel"
                  defaultValue={policy.channel} aria-describedby="channel-help"
                >
                  {CHANNELS.map(channel => <option key={channel} value={channel}>
                    {ui.t(`catalog.channel.${channel}` as MessageKey)}
                  </option>)}
                </select>
              </Field>
              <Field id="mode" label={ui.t("catalog.modeLabel")}>
                <select
                  className="field-control" id="mode" name="mode" defaultValue={policy.mode}
                >
                  {MODES.map(mode => <option key={mode} value={mode}>
                    {ui.t(`catalog.mode.${mode}` as MessageKey)}
                  </option>)}
                </select>
              </Field>
              <div>
                <button type="submit" className="button">{ui.t("catalog.policySubmit")}</button>
              </div>
            </form>
            : <Hint>{ui.t("catalog.readOnlyNotice")}</Hint>}
        </Panel>
      </Reveal>
    </ConsoleShell>;
  } catch (error) {
    return <ConsoleProblem
      chrome={chrome} error={error} area={chrome.ui.t("catalog.area")} active="catalog"
      back={{ href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace") }}
    />;
  }
}
