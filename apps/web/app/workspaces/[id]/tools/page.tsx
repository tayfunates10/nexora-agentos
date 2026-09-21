import { redirect } from "next/navigation";
import { z } from "zod";
import { api } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { consoleChrome } from "../../../../lib/server/chrome";
import {
  forcesApproval, policyDecisionSchema, policyKey, policyLabelKey, policyTone,
  sideEffectKey, toolPageSchema,
} from "../../../../lib/tool-contracts";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { formatTimestamp } from "../../../../lib/i18n/format.ts";
import { ConsoleBreadcrumb, ConsoleShell } from "../../../../components/shell/ConsoleShell.tsx";
import {
  CodeBlock, Detail, DetailList, Disclosure, EmptyState, Field, Hint, Notice,
  PageHeader, Pagination, StatusBadge,
} from "../../../../components/ui/primitives.tsx";
import { TextWithLink } from "../../../../components/ui/RichText.tsx";
import { ConsoleProblem, Failed, InvalidLinkPage, Saved } from "../console";
import type { MessageKey } from "../../../../messages/en.ts";

const MESSAGES: Record<string, MessageKey> = {
  invalid: "tools.error.invalid",
  forbidden: "tools.error.forbidden",
  failed: "tools.error.failed",
};

export default async function Tools({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string; saved?: string; error?: string }>;
}) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const root = `/workspaces/${id}/tools`;
  let chrome = await consoleChrome(session);
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !/^[a-z][a-z0-9_.-]{1,63}$/.test(query.cursor))) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace"),
    }}/>;
  }

  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
    const tools = await api(
      session,
      `/api/v1/workspaces/${id}/tools?limit=25${query.cursor ? "&cursor=" + query.cursor : ""}`,
      toolPageSchema,
    );
    const canManage = workspace.role !== "member";

    return <ConsoleShell
      chrome={chrome}
      active="tools"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace} trail={[{ href: root, label: ui.t("navigation.tools") }]}
      />}
    >
      <PageHeader
        eyebrow={ui.t("tools.eyebrow")}
        title={ui.t("tools.title")}
        intro={ui.t("tools.intro")}
      />
      <Hint>{ui.t("tools.registrationNotice")}</Hint>
      <Saved message={query.saved === "policy" ? ui.t("tools.saved") : null}/>
      <Failed message={query.error ? ui.t(MESSAGES[query.error] ?? MESSAGES.failed) : null}/>

      {tools.items.length === 0
        ? <EmptyState title={ui.t("tools.emptyTitle")}><p>{ui.t("tools.emptyBody")}</p></EmptyState>
        : <div className="card-list">{tools.items.map(tool => <article className="card" key={tool.id}>
          <StatusBadge tone={policyTone(tool)}>{ui.t(policyLabelKey(tool))}</StatusBadge>
          {/* The tool name and its description are registered content, shown as stored. */}
          <h2 className="section-title">{tool.name}</h2>
          <p className="notice">{tool.description}</p>
          <DetailList>
            <Detail label={ui.t("tools.sideEffect")}>{ui.t(sideEffectKey(tool.side_effect))}</Detail>
            <Detail label={ui.t("tools.server")}>
              <code>{tool.server_key}</code> · <code>{tool.remote_name}</code></Detail>
            <Detail label={ui.t("tools.enabled")}>
              {ui.t(tool.enabled ? "tools.enabledYes" : "tools.enabledNo")}</Detail>
            {tool.policy_reason && <Detail label={ui.t("tools.policyReason")}>
              {tool.policy_reason}</Detail>}
            {tool.policy_updated_at && <Detail label={ui.t("tools.policySet")}>
              <time dateTime={tool.policy_updated_at}>
                {formatTimestamp(tool.policy_updated_at, ui.locale)}</time></Detail>}
          </DetailList>
          {forcesApproval(tool) && <Notice tone="warning">
            <TextWithLink
              ui={ui} message="tools.forcesApproval" link="tools.forcesApprovalLink"
              href={`/workspaces/${id}/approvals`}
            />
          </Notice>}
          <Disclosure summary={ui.t("tools.inputContract")}>
            <CodeBlock label={ui.t("tools.inputContract")}>
              {JSON.stringify(tool.input_schema, null, 2)}
            </CodeBlock>
          </Disclosure>
          {canManage && <form className="form" action="/workspaces/tools/policy" method="post">
            <input type="hidden" name="csrf" value={session.csrf}/>
            <input type="hidden" name="workspace" value={id}/>
            <input type="hidden" name="tool" value={tool.name}/>
            <div className="form-row">
              <Field id={`decision-${tool.id}`} label={ui.t("tools.decisionLabel")}>
                <select
                  className="field-control" id={`decision-${tool.id}`} name="decision"
                  defaultValue={tool.policy_decision ?? "deny"}
                >{policyDecisionSchema.options.map(option => <option key={option} value={option}>
                  {ui.t(policyKey(option))}
                </option>)}</select>
              </Field>
              <Field
                id={`reason-${tool.id}`}
                label={ui.t("tools.reasonLabel")}
                help={ui.t("tools.reasonHelp")}
              >
                <input
                  className="field-control" id={`reason-${tool.id}`} name="reason" required
                  maxLength={500} autoComplete="off" defaultValue={tool.policy_reason ?? ""}
                  aria-describedby={`reason-${tool.id}-help`}
                />
              </Field>
              <button type="submit" className="button">{ui.t("tools.savePolicy")}</button>
            </div>
          </form>}
        </article>)}</div>}

      <Pagination
        label={ui.t("tools.pagesLabel")}
        previous={query.cursor ? { href: root, label: ui.t("common.firstPage") } : null}
        next={tools.next_cursor
          ? { href: `${root}?cursor=${tools.next_cursor}`, label: ui.t("tools.more") }
          : null}
      />
      {!canManage && <Hint>{ui.t("tools.readOnlyNotice")}</Hint>}
    </ConsoleShell>;
  } catch (error) {
    return <ConsoleProblem
      chrome={chrome} error={error} area={chrome.ui.t("tools.area")} active="tools"
      back={{ href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace") }}
    />;
  }
}
