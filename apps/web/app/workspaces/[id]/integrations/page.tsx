import { redirect } from "next/navigation";
import { z } from "zod";
import { api } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { consoleChrome } from "../../../../lib/server/chrome";
import {
  INTEGRATION_STATUS_TONES, definitionPageSchema, integrationPageSchema,
  integrationStatusKey, orderedFields, scopeReport,
  type CredentialField, type IntegrationDefinition, type TenantIntegration,
} from "../../../../lib/integration-contracts.ts";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { formatTimestamp } from "../../../../lib/i18n/format.ts";
import type { Ui } from "../../../../lib/i18n/messages.ts";
import { ConsoleBreadcrumb, ConsoleShell } from "../../../../components/shell/ConsoleShell.tsx";
import {
  Badge, Detail, DetailList, Disclosure, EmptyState, Field, Hint, Notice, PageHeader,
  Panel, StatusBadge,
} from "../../../../components/ui/primitives.tsx";
import { Reveal } from "../../../../components/ui/Reveal.tsx";
import { ConsoleProblem, Failed, InvalidLinkPage, Saved } from "../console";
import type { MessageKey } from "../../../../messages/en.ts";

const MESSAGES: Record<string, MessageKey> = {
  invalid: "integrations.error.invalid",
  forbidden: "integrations.error.forbidden",
  conflict: "integrations.error.conflict",
  vault: "integrations.error.vault",
  failed: "integrations.error.failed",
};
const SAVED: Record<string, MessageKey> = {
  connected: "integrations.connected",
  rotated: "integrations.rotated",
  removed: "integrations.removed",
  updated: "integrations.updated",
  tested: "integrations.tested",
};

/**
 * One credential input, built from what the connector says it needs. A field marked
 * secret is a password input that starts empty and is never pre-filled from the server,
 * because the server does not have the value to pre-fill it with.
 */
function CredentialInput({ id, field, ui }: { id: string; field: CredentialField; ui: Ui }) {
  return <Field key={field.key} id={id} label={field.label} help={field.help || undefined}>
    <input
      className="field-control"
      id={id}
      name={"field_" + field.key}
      type={field.secret ? "password" : "text"}
      required={field.required}
      maxLength={8192}
      autoComplete={field.secret ? "new-password" : "off"}
      pattern={field.pattern ?? undefined}
      aria-describedby={field.help ? `${id}-help` : undefined}
      placeholder={field.secret ? ui.t("integrations.hintLabel") : undefined}
    />
  </Field>;
}

function ConnectForm({ definition, workspaceId, csrf, ui }: {
  definition: IntegrationDefinition; workspaceId: string; csrf: string; ui: Ui;
}) {
  if (definition.auth_type === "oauth2") {
    return <>
      <Hint>{ui.t("integrations.oauthNotice", { service: definition.name })}</Hint>
      <form className="form" action="/workspaces/integrations/mutate" method="post">
        <input type="hidden" name="csrf" value={csrf}/>
        <input type="hidden" name="workspace" value={workspaceId}/>
        <input type="hidden" name="operation" value="oauth"/>
        <input type="hidden" name="definition" value={definition.id}/>
        <Field id={`oauth-name-${definition.id}`} label={ui.t("integrations.displayNameLabel")}>
          <input
            className="field-control" id={`oauth-name-${definition.id}`} name="display_name"
            required maxLength={100} autoComplete="off"
          />
        </Field>
        <Field
          id={`oauth-account-${definition.id}`}
          label={ui.t("integrations.accountLabel")}
          help={ui.t("integrations.accountHelp")}
        >
          <input
            className="field-control" id={`oauth-account-${definition.id}`}
            name="account_identifier" required maxLength={200} autoComplete="off"
            aria-describedby={`oauth-account-${definition.id}-help`}
          />
        </Field>
        <div>
          <button type="submit" className="button">{ui.t("integrations.connectSubmit")}</button>
        </div>
      </form>
    </>;
  }
  return <form className="form" action="/workspaces/integrations/mutate" method="post">
    <input type="hidden" name="csrf" value={csrf}/>
    <input type="hidden" name="workspace" value={workspaceId}/>
    <input type="hidden" name="operation" value="connect"/>
    <input type="hidden" name="definition" value={definition.id}/>
    <Field id={`name-${definition.id}`} label={ui.t("integrations.displayNameLabel")}
      help={ui.t("integrations.displayNameHelp")}>
      <input
        className="field-control" id={`name-${definition.id}`} name="display_name" required
        maxLength={100} autoComplete="off" aria-describedby={`name-${definition.id}-help`}
      />
    </Field>
    <Field id={`account-${definition.id}`} label={ui.t("integrations.accountLabel")}
      help={ui.t("integrations.accountHelp")}>
      <input
        className="field-control" id={`account-${definition.id}`} name="account_identifier"
        required maxLength={200} autoComplete="off"
        aria-describedby={`account-${definition.id}-help`}
      />
    </Field>
    {orderedFields(definition).map(field => <CredentialInput
      key={field.key} id={`${definition.id}-${field.key}`} field={field} ui={ui}
    />)}
    <div><button type="submit" className="button">{ui.t("integrations.connectSubmit")}</button></div>
  </form>;
}

function ConnectionCard({ integration, definition, workspaceId, csrf, canManage, ui }: {
  integration: TenantIntegration;
  definition: IntegrationDefinition | undefined;
  workspaceId: string; csrf: string; canManage: boolean; ui: Ui;
}) {
  const { granted, missing } = scopeReport(integration);
  const base = { csrf, workspace: workspaceId, integration: integration.id };
  return <article className="card">
    <StatusBadge tone={INTEGRATION_STATUS_TONES[integration.status]}>
      {ui.t(integrationStatusKey(integration.status))}
    </StatusBadge>
    <h3 className="section-title">{integration.display_name}</h3>
    <p className="field-help">
      {definition?.name ?? integration.integration_definition_id}
      {" · "}
      {ui.t("integrations.accountOf", { account: integration.account_identifier })}
    </p>
    <p className="notice">{ui.t(`integrations.statusHelp.${integration.status}` as MessageKey)}</p>

    <DetailList>
      <Detail label={ui.t("integrations.hintLabel")}>
        {/* The masked hint is the whole of what the platform will ever show. */}
        <code>{integration.credential_hint ?? ui.t("common.empty")}</code>
      </Detail>
      <Detail label={ui.t("integrations.lastTested")}>
        {integration.last_tested_at
          ? <time dateTime={integration.last_tested_at}>
            {formatTimestamp(integration.last_tested_at, ui.locale)}</time>
          : ui.t("integrations.never")}
      </Detail>
      <Detail label={ui.t("integrations.lastSuccess")}>
        {integration.last_success_at
          ? <time dateTime={integration.last_success_at}>
            {formatTimestamp(integration.last_success_at, ui.locale)}</time>
          : ui.t("integrations.never")}
      </Detail>
      {integration.last_error && <Detail label={ui.t("integrations.lastError")}>
        <code>{integration.last_error}</code>
      </Detail>}
      <Detail label={ui.t("integrations.scopesGranted")}>
        {granted.length ? granted.join(", ") : ui.t("common.none")}
      </Detail>
      {missing.length > 0 && <Detail label={ui.t("integrations.scopesMissing")}>
        <code>{missing.join(", ")}</code>
      </Detail>}
    </DetailList>

    <form className="form" action="/workspaces/integrations/mutate" method="post">
      {Object.entries({ ...base, operation: "test" }).map(([name, value]) =>
        <input key={name} type="hidden" name={name} value={value}/>)}
      <button type="submit" className="button secondary small">{ui.t("integrations.test")}</button>
    </form>

    {canManage && <>
      <form className="form" action="/workspaces/integrations/mutate" method="post">
        {Object.entries({
          ...base,
          operation: "toggle",
          enabled: integration.status === "disabled" ? "true" : "false",
        }).map(([name, value]) =>
          <input key={name} type="hidden" name={name} value={value}/>)}
        <button type="submit" className="button secondary small">
          {ui.t(integration.status === "disabled"
            ? "integrations.enable" : "integrations.disable")}
        </button>
      </form>

      {definition && definition.auth_type !== "oauth2" && <Disclosure
        summary={ui.t("integrations.rotateTitle")}
      >
        <Hint>{ui.t("integrations.rotateHelp")}</Hint>
        <form className="form" action="/workspaces/integrations/mutate" method="post">
          {Object.entries({ ...base, operation: "rotate" }).map(([name, value]) =>
            <input key={name} type="hidden" name={name} value={value}/>)}
          {orderedFields(definition).map(field => <CredentialInput
            key={field.key} id={`rotate-${integration.id}-${field.key}`} field={field} ui={ui}
          />)}
          <div>
            <button type="submit" className="button">{ui.t("integrations.rotateSubmit")}</button>
          </div>
        </form>
      </Disclosure>}

      <Disclosure summary={ui.t("integrations.disconnect")}>
        <Hint>{ui.t("integrations.disconnectHelp")}</Hint>
        <form className="form" action="/workspaces/integrations/mutate" method="post">
          {Object.entries({ ...base, operation: "disconnect" }).map(([name, value]) =>
            <input key={name} type="hidden" name={name} value={value}/>)}
          <button type="submit" className="button danger small">
            {ui.t("integrations.disconnect")}
          </button>
        </form>
      </Disclosure>
    </>}
  </article>;
}

export default async function Integrations({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ saved?: string; error?: string }>;
}) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const root = `/workspaces/${id}/integrations`;
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
    const [connections, catalog] = await Promise.all([
      api(session, `/api/v1/workspaces/${id}/integrations?limit=50`, integrationPageSchema),
      api(session, "/api/v1/integration-catalog?limit=100", definitionPageSchema),
    ]);
    const definitions = new Map(catalog.items.map(item => [item.id, item]));
    const canManage = workspace.role !== "member";

    return <ConsoleShell
      chrome={chrome}
      active="integrations"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace}
        trail={[{ href: root, label: ui.t("navigation.integrations") }]}
      />}
    >
      <PageHeader
        eyebrow={ui.t("integrations.eyebrow")}
        title={ui.t("integrations.title")}
        intro={ui.t("integrations.intro")}
      />
      <Notice>{ui.t("integrations.secretNotice")}</Notice>
      <Saved message={query.saved ? ui.t(SAVED[query.saved] ?? SAVED.updated) : null}/>
      <Failed message={query.error ? ui.t(MESSAGES[query.error] ?? MESSAGES.failed) : null}/>

      {connections.items.length === 0
        ? <EmptyState title={ui.t("integrations.emptyTitle")}>
          <p>{ui.t(canManage ? "integrations.emptyManage" : "integrations.emptyMember")}</p>
        </EmptyState>
        : <div className="card-list">{connections.items.map(integration => <ConnectionCard
          key={integration.id}
          integration={integration}
          definition={definitions.get(integration.integration_definition_id)}
          workspaceId={id}
          csrf={session.csrf}
          canManage={canManage}
          ui={ui}
        />)}</div>}

      <Reveal>
        <Panel labelledBy="connect-title">
          <h2 id="connect-title" className="section-title">
            {ui.t("integrations.catalogTitle")}
          </h2>
          {canManage
            ? <div className="card-list">{catalog.items.map(definition => <article
              className="card" key={definition.id}
            >
              <Badge>{ui.t(`integrations.authType.${definition.auth_type}` as MessageKey)}</Badge>
              {definition.status !== "available" && <Badge accent>
                {ui.t(`integrations.definitionStatus.${definition.status}` as MessageKey)}
              </Badge>}
              <h3 className="section-title">{definition.name}</h3>
              <p className="notice">{definition.description}</p>
              {definition.capabilities.length > 0 && <p className="field-help">
                {ui.t("integrations.capabilities")}: {definition.capabilities.join(", ")}
              </p>}
              <Disclosure summary={ui.t("integrations.connectTitle")}>
                <ConnectForm
                  definition={definition} workspaceId={id} csrf={session.csrf} ui={ui}
                />
              </Disclosure>
            </article>)}</div>
            : <Hint>{ui.t("integrations.readOnlyNotice")}</Hint>}
        </Panel>
      </Reveal>
    </ConsoleShell>;
  } catch (error) {
    return <ConsoleProblem
      chrome={chrome} error={error} area={chrome.ui.t("integrations.area")} active="integrations"
      back={{ href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace") }}
    />;
  }
}
