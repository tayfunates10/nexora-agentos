import { redirect } from "next/navigation";
import { api, ApiError } from "../../../lib/server/api";
import { currentSession } from "../../../lib/server/session";
import { consoleChrome } from "../../../lib/server/chrome";
import {
  ROLLOUT_STATE_TONES, catalogAgentPageSchema, catalogVersionPageSchema, nextStep,
  rolloutPageSchema, rolloutStateKey, type CatalogAgent,
} from "../../../lib/studio-contracts.ts";
import { formatTimestamp } from "../../../lib/i18n/format.ts";
import type { Ui } from "../../../lib/i18n/messages.ts";
import { ConsoleShell } from "../../../components/shell/ConsoleShell.tsx";
import {
  Badge, Detail, DetailList, Disclosure, EmptyState, Field, Hint, Notice, PageHeader,
  Panel, StatusBadge,
} from "../../../components/ui/primitives.tsx";
import { Reveal } from "../../../components/ui/Reveal.tsx";
import type { MessageKey } from "../../../messages/en.ts";

const MESSAGES: Record<string, MessageKey> = {
  invalid: "studio.error.invalid",
  manifest: "studio.error.manifest",
  conflict: "studio.error.conflict",
  forbidden: "studio.error.forbidden",
  failed: "studio.error.failed",
};
const STATUSES = ["draft", "beta", "stable", "deprecated", "disabled"] as const;
const CHANNELS = ["stable", "beta", "canary"] as const;
const VISIBILITY = ["restricted", "public"] as const;

/** One catalog entry: its published versions, its rollouts and who may see it. */
async function AgentPanel({ agent, csrf, ui }: { agent: CatalogAgent; csrf: string; ui: Ui }) {
  const session = await currentSession();
  if (!session) redirect("/login");
  const base = `/api/v1/platform/agents/${agent.slug}`;
  const [versions, rollouts] = await Promise.all([
    api(session, `${base}/versions?limit=10`, catalogVersionPageSchema),
    api(session, `${base}/rollouts?limit=25`, rolloutPageSchema),
  ]);
  const hidden = { csrf, slug: agent.slug };
  const active = rollouts.items.filter(item => item.state === "active");

  return <article className="card">
    <Badge>{ui.t(`studio.agentStatus.${agent.status}` as MessageKey)}</Badge>
    <Badge accent={agent.visibility === "public"}>
      {ui.t(`studio.visibility.${agent.visibility}` as MessageKey)}
    </Badge>
    <h3 className="section-title">{agent.name}</h3>
    <p className="field-help">{agent.slug}</p>
    <p className="notice">{agent.description}</p>
    <DetailList>
      <Detail label={ui.t("studio.versionsTitle")}>
        {agent.latest_version
          ? `${ui.t("studio.latestVersion", { version: agent.latest_version })} · ${
            ui.t("studio.versionCount", { count: String(agent.version_count) })}`
          : ui.t("studio.noVersions")}
      </Detail>
      {versions.items.map(version => <Detail key={version.id} label={version.version}>
        {ui.t(`studio.agentStatus.${version.status}` as MessageKey)}
        {" · "}
        {ui.t(`catalog.channel.${version.channel}` as MessageKey)}
        {" · "}
        <time dateTime={version.created_at}>
          {formatTimestamp(version.created_at, ui.locale)}
        </time>
        {version.changelog && <> · {version.changelog}</>}
      </Detail>)}
    </DetailList>

    <Disclosure summary={ui.t("studio.publishTitle")}>
      <Hint>{ui.t("studio.publishHelp")}</Hint>
      <form className="form" action="/platform/agents/mutate" method="post">
        {Object.entries({ ...hidden, operation: "publish" }).map(([name, value]) =>
          <input key={name} type="hidden" name={name} value={value}/>)}
        <Field id={`manifest-${agent.slug}`} label={ui.t("studio.manifestLabel")}>
          <textarea
            className="field-control" id={`manifest-${agent.slug}`} name="manifest" required
            rows={10} maxLength={200000} spellCheck={false}
          />
        </Field>
        <div><button type="submit" className="button">{ui.t("studio.publishSubmit")}</button></div>
      </form>
    </Disclosure>

    <Disclosure summary={ui.t("studio.rolloutsTitle")}>
      <Hint>{ui.t("studio.rolloutHelp")}</Hint>
      <DetailList>{rollouts.items.map(rollout => <Detail
        key={rollout.id}
        label={ui.t(rolloutStateKey(rollout.state))}
      >
        <StatusBadge tone={ROLLOUT_STATE_TONES[rollout.state]}>
          {ui.t("studio.rolloutLine", {
            version: rollout.version,
            channel: ui.t(`catalog.channel.${rollout.channel}` as MessageKey),
            percentage: String(rollout.percentage),
          })}
        </StatusBadge>
      </Detail>)}</DetailList>

      {active.map(rollout => <div className="button-row" key={rollout.id}>
        {nextStep(rollout.percentage) !== null && <form
          action="/platform/agents/mutate" method="post"
        >
          {Object.entries({
            ...hidden, operation: "widen", rollout: rollout.id,
            percentage: String(nextStep(rollout.percentage)),
          }).map(([name, value]) => <input key={name} type="hidden" name={name} value={value}/>)}
          <button type="submit" className="button">
            {ui.t("studio.widen", { percentage: String(nextStep(rollout.percentage)) })}
          </button>
        </form>}
        <form action="/platform/agents/mutate" method="post">
          {Object.entries({ ...hidden, operation: "pause", rollout: rollout.id })
            .map(([name, value]) => <input key={name} type="hidden" name={name} value={value}/>)}
          <button type="submit" className="button secondary">{ui.t("studio.pause")}</button>
        </form>
        <form action="/platform/agents/mutate" method="post">
          {Object.entries({ ...hidden, operation: "rollback", rollout: rollout.id })
            .map(([name, value]) => <input key={name} type="hidden" name={name} value={value}/>)}
          <button type="submit" className="button danger">{ui.t("studio.rollback")}</button>
        </form>
      </div>)}
      {active.length > 0 && <Hint>{ui.t("studio.rollbackHelp")}</Hint>}

      <form className="form" action="/platform/agents/mutate" method="post">
        {Object.entries({ ...hidden, operation: "rollout" }).map(([name, value]) =>
          <input key={name} type="hidden" name={name} value={value}/>)}
        <Field id={`rollout-version-${agent.slug}`} label={ui.t("studio.rolloutVersionLabel")}>
          <input
            className="field-control" id={`rollout-version-${agent.slug}`} name="version" required
            pattern="[0-9]+\.[0-9]+\.[0-9]+" autoComplete="off"
            defaultValue={agent.latest_version ?? ""}
          />
        </Field>
        <Field id={`rollout-channel-${agent.slug}`} label={ui.t("studio.rolloutChannelLabel")}>
          <select className="field-control" id={`rollout-channel-${agent.slug}`} name="channel">
            {CHANNELS.map(channel => <option key={channel} value={channel}>
              {ui.t(`catalog.channel.${channel}` as MessageKey)}
            </option>)}
          </select>
        </Field>
        <Field id={`rollout-percent-${agent.slug}`} label={ui.t("studio.rolloutPercentLabel")}>
          <input
            className="field-control" id={`rollout-percent-${agent.slug}`} name="percentage"
            type="number" min={0} max={100} defaultValue={5} required
          />
        </Field>
        <div><button type="submit" className="button">{ui.t("studio.rolloutSubmit")}</button></div>
      </form>
    </Disclosure>

    <Disclosure summary={ui.t("studio.entitlementsTitle")}>
      <Hint>{ui.t("studio.entitlementsHelp")}</Hint>
      <form className="form" action="/platform/agents/mutate" method="post">
        {Object.entries({ ...hidden, operation: "grant" }).map(([name, value]) =>
          <input key={name} type="hidden" name={name} value={value}/>)}
        <Field id={`grant-${agent.slug}`} label={ui.t("studio.workspaceLabel")}>
          <input
            className="field-control" id={`grant-${agent.slug}`} name="workspace" required
            autoComplete="off"
          />
        </Field>
        <div><button type="submit" className="button">{ui.t("studio.grantSubmit")}</button></div>
      </form>
    </Disclosure>
  </article>;
}

export default async function AgentStudio({ searchParams }: {
  searchParams: Promise<{ saved?: string; error?: string }>;
}) {
  const session = await currentSession();
  if (!session) redirect("/login");
  const query = await searchParams;
  const chrome = await consoleChrome(session);
  const { ui } = chrome;

  let agents;
  try {
    agents = await api(session, "/api/v1/platform/agents?limit=50", catalogAgentPageSchema);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) redirect("/login?error=session_expired");
    // The platform surface answers 404 to everyone who is not an administrator, so this
    // page says the same thing rather than confirming that the surface exists.
    return <ConsoleShell chrome={chrome}>
      <h1 className="page-title">{ui.t("studio.unavailableTitle")}</h1>
      <Notice live="alert" tone="warning">{ui.t("studio.unavailableBody")}</Notice>
      <p><a className="link" href="/workspaces">{ui.t("workspaces.backToWorkspaces")}</a></p>
    </ConsoleShell>;
  }

  return <ConsoleShell chrome={chrome}>
    <PageHeader
      eyebrow={ui.t("studio.eyebrow")}
      title={ui.t("studio.title")}
      intro={ui.t("studio.intro")}
    />
    {query.saved && <Notice live="status" tone="success">
      {ui.t(query.saved === "published" ? "studio.published" : "studio.saved")}
    </Notice>}
    {query.error && <Notice live="alert" tone="danger">
      {ui.t(MESSAGES[query.error] ?? MESSAGES.failed)}
    </Notice>}

    {agents.items.length === 0
      ? <EmptyState title={ui.t("studio.emptyTitle")}>
        <p>{ui.t("studio.emptyBody")}</p>
      </EmptyState>
      : <div className="card-list">{agents.items.map(agent => <AgentPanel
        key={agent.id} agent={agent} csrf={session.csrf} ui={ui}
      />)}</div>}

    <Reveal>
      <Panel labelledBy="create-catalog-title">
        <h2 id="create-catalog-title" className="section-title">{ui.t("studio.createTitle")}</h2>
        <form className="form" action="/platform/agents/mutate" method="post">
          <input type="hidden" name="csrf" value={session.csrf}/>
          <input type="hidden" name="operation" value="create"/>
          <Field id="slug" label={ui.t("studio.slugLabel")} help={ui.t("studio.slugHelp")}>
            <input
              className="field-control" id="slug" name="slug" required minLength={3} maxLength={64}
              pattern="[a-z][a-z0-9]*(-[a-z0-9]+)*" autoComplete="off"
              aria-describedby="slug-help"
            />
          </Field>
          <Field id="name" label={ui.t("studio.nameLabel")}>
            <input
              className="field-control" id="name" name="name" required maxLength={100}
              autoComplete="off"
            />
          </Field>
          <Field id="description" label={ui.t("studio.descriptionLabel")}>
            <textarea
              className="field-control" id="description" name="description" required rows={3}
              maxLength={1000}
            />
          </Field>
          <Field id="category" label={ui.t("studio.categoryLabel")}>
            <input
              className="field-control" id="category" name="category" required maxLength={40}
              pattern="[a-z][a-z0-9_\-]{1,39}" autoComplete="off"
            />
          </Field>
          <Field id="icon" label={ui.t("studio.iconLabel")}>
            <input
              className="field-control" id="icon" name="icon" required maxLength={40}
              pattern="[a-z][a-z0-9_\-]{1,39}" autoComplete="off"
            />
          </Field>
          <Field id="status" label={ui.t("studio.statusLabel")}>
            <select className="field-control" id="status" name="status" defaultValue="draft">
              {STATUSES.map(status => <option key={status} value={status}>
                {ui.t(`studio.agentStatus.${status}` as MessageKey)}
              </option>)}
            </select>
          </Field>
          <Field id="visibility" label={ui.t("studio.visibilityLabel")}>
            <select
              className="field-control" id="visibility" name="visibility"
              defaultValue="restricted"
            >
              {VISIBILITY.map(value => <option key={value} value={value}>
                {ui.t(`studio.visibility.${value}` as MessageKey)}
              </option>)}
            </select>
          </Field>
          <div><button type="submit" className="button">{ui.t("studio.createSubmit")}</button></div>
        </form>
      </Panel>
    </Reveal>
  </ConsoleShell>;
}
