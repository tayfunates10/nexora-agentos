import { redirect } from "next/navigation";
import { z } from "zod";
import { api } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { consoleChrome } from "../../../../lib/server/chrome";
import {
  APPROVAL_STATUS_TONES, approvalPageSchema, approvalStatusKey, approvalStatusSchema,
  formatArguments, remainingTime, type ToolApproval,
} from "../../../../lib/tool-contracts";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { formatTimestamp } from "../../../../lib/i18n/format.ts";
import type { Ui } from "../../../../lib/i18n/messages.ts";
import { ConsoleBreadcrumb, ConsoleShell } from "../../../../components/shell/ConsoleShell.tsx";
import {
  CodeBlock, Detail, DetailList, Disclosure, EmptyState, FilterLink, Filters, Hint,
  Notice, PageHeader, Pagination, StatusBadge,
} from "../../../../components/ui/primitives.tsx";
import { TextWithLink } from "../../../../components/ui/RichText.tsx";
import { ConsoleProblem, Failed, InvalidLinkPage, Saved } from "../console";
import type { MessageKey } from "../../../../messages/en.ts";

const MESSAGES: Record<string, MessageKey> = {
  forbidden: "approvals.error.forbidden",
  gone: "approvals.error.gone",
  failed: "approvals.error.failed",
};

/** A closed approval never counts down; an expired one says so instead of going negative. */
function remainingLabel(ui: Ui, approval: ToolApproval): string {
  const remaining = remainingTime(approval);
  if (remaining.unit === "expired") return ui.t("approvals.expired");
  const key = `approvals.${remaining.unit}Left` as MessageKey;
  return ui.t(key, { count: remaining.count });
}

function Approval({ ui, approval, workspaceId, csrf, canDecide }: {
  ui: Ui; approval: ToolApproval; workspaceId: string; csrf: string; canDecide: boolean;
}) {
  const open = approval.status === "pending";
  return <article className="card">
    <StatusBadge tone={APPROVAL_STATUS_TONES[approval.status]}>
      {ui.t(approvalStatusKey(approval.status))}
      {open ? ` · ${remainingLabel(ui, approval)}` : ""}
    </StatusBadge>
    {/* The requested action and the policy reason are recorded values, shown as stored. */}
    <h3 className="section-title">{approval.requested_action}</h3>
    <p className="notice">{approval.policy_reason}</p>
    <DetailList>
      <Detail label={ui.t("approvals.requestedBy")}>
        <code>{approval.requester_subject}</code></Detail>
      <Detail label={ui.t("approvals.run")}>
        <a className="link" href={`/workspaces/${workspaceId}/runs/${approval.run_id}`}>
          <code>{approval.run_id}</code></a></Detail>
      <Detail label={ui.t("approvals.requested")}>
        <time dateTime={approval.created_at}>
          {formatTimestamp(approval.created_at, ui.locale)}</time></Detail>
      <Detail label={ui.t(open ? "approvals.expires" : "approvals.decided")}>
        {open
          ? <time dateTime={approval.expires_at}>
            {formatTimestamp(approval.expires_at, ui.locale)}</time>
          : <>
            <time dateTime={approval.decided_at!}>
              {formatTimestamp(approval.decided_at!, ui.locale)}</time>
            {approval.approver_subject && <> · <code>{approval.approver_subject}</code></>}
          </>}
      </Detail>
    </DetailList>
    <Disclosure summary={ui.t("approvals.arguments")} open={open}>
      {/* Model-proposed arguments, exactly as normalized and stored. */}
      <CodeBlock label={ui.t("approvals.arguments")}>
        {formatArguments(approval.normalized_arguments)}
      </CodeBlock>
    </Disclosure>
    {!open
      ? <Hint>{ui.t("approvals.closedNotice")}</Hint>
      : canDecide
        ? <div className="stack">
          <Notice tone="warning">{ui.t("approvals.decisionNotice")}</Notice>
          <form className="button-row" action="/workspaces/approvals/decide" method="post">
            <input type="hidden" name="csrf" value={csrf}/>
            <input type="hidden" name="workspace" value={workspaceId}/>
            <input type="hidden" name="approval" value={approval.id}/>
            <button type="submit" name="decision" value="approved" className="button">
              {ui.t("approvals.approve")}</button>
            <button type="submit" name="decision" value="rejected" className="button danger">
              {ui.t("approvals.reject")}</button>
          </form>
        </div>
        : <Hint>{ui.t("approvals.readOnlyNotice")}</Hint>}
  </article>;
}

export default async function Approvals({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string; status?: string; decided?: string; error?: string }>;
}) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const status = query.status ? approvalStatusSchema.safeParse(query.status) : null;
  const root = `/workspaces/${id}/approvals`;
  let chrome = await consoleChrome(session);
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !z.uuid().safeParse(query.cursor).success)
    || (status !== null && !status.success)) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace"),
    }}/>;
  }
  const selected = status?.success ? status.data : "pending";

  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
    const approvals = await api(
      session,
      `/api/v1/workspaces/${id}/approvals?limit=25&status=${selected}`
        + (query.cursor ? "&cursor=" + query.cursor : ""),
      approvalPageSchema,
    );
    // Only owners and admins decide. The API enforces that on every decision; the console
    // simply does not offer a control the reader could not use.
    const canDecide = workspace.role !== "member";

    return <ConsoleShell
      chrome={chrome}
      active="approvals"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace} trail={[{ href: root, label: ui.t("navigation.approvals") }]}
      />}
    >
      <PageHeader
        eyebrow={ui.t("approvals.eyebrow")}
        title={ui.t("approvals.title")}
        intro={ui.t("approvals.intro")}
      />
      <Notice tone="warning">{ui.t("approvals.alwaysNotice")}</Notice>
      <Saved message={query.decided === "approved" ? ui.t("approvals.approvedNotice")
        : query.decided === "rejected" ? ui.t("approvals.rejectedNotice") : null}/>
      <Failed message={query.error ? ui.t(MESSAGES[query.error] ?? MESSAGES.failed) : null}/>

      <Filters label={ui.t("approvals.filterLabel")}>
        {approvalStatusSchema.options.map(option => <FilterLink
          key={option}
          href={option === "pending" ? root : `${root}?status=${option}`}
          current={selected === option}
        >{ui.t(approvalStatusKey(option))}</FilterLink>)}
      </Filters>

      {approvals.items.length === 0
        ? <EmptyState title={ui.t(selected === "pending"
          ? "approvals.emptyPendingTitle" : "approvals.emptyFilterTitle")}>
          <p>{ui.t(selected === "pending"
            ? "approvals.emptyPendingBody" : "approvals.emptyFilterBody")}</p>
        </EmptyState>
        : <div className="card-list">{approvals.items.map(approval => <Approval
          key={approval.id} ui={ui} approval={approval} workspaceId={id}
          csrf={session.csrf} canDecide={canDecide}
        />)}</div>}

      <Pagination
        label={ui.t("approvals.pagesLabel")}
        previous={query.cursor
          ? {
            href: selected === "pending" ? root : `${root}?status=${selected}`,
            label: ui.t("common.firstPage"),
          }
          : null}
        next={approvals.next_cursor
          ? {
            href: `${root}?status=${selected}&cursor=${approvals.next_cursor}`,
            label: ui.t("approvals.more"),
          }
          : null}
      />
      <Hint><TextWithLink
        ui={ui} message="approvals.toolsHint" link="approvals.toolsLink"
        href={`/workspaces/${id}/tools`}
      /></Hint>
    </ConsoleShell>;
  } catch (error) {
    return <ConsoleProblem
      chrome={chrome} error={error} area={chrome.ui.t("approvals.area")} active="approvals"
      back={{ href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace") }}
    />;
  }
}
