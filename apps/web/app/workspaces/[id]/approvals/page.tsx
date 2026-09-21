import { redirect } from "next/navigation";
import { z } from "zod";
import { api } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import {
  APPROVAL_STATUS_LABELS,
  APPROVAL_STATUS_TONES,
  approvalPageSchema,
  approvalStatusSchema,
  expiresIn,
  formatArguments,
  toolTimestamp,
  type ToolApproval,
} from "../../../../lib/tool-contracts";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { ConsoleError, Failed, InvalidLink, Saved } from "../console";

const MESSAGES: Record<string, string> = {
  forbidden: "Only workspace owners and admins can decide tool approvals.",
  gone: "That approval is no longer open. It may have expired, or its run may have been cancelled.",
  failed: "The decision could not be recorded. Reload the page and check the current state.",
};

function Approval({ approval, workspaceId, csrf }: {
  approval: ToolApproval; workspaceId: string; csrf: string;
}) {
  const open = approval.status === "pending";
  return <article className="card approval-card">
    <p className={"state " + APPROVAL_STATUS_TONES[approval.status]}>
      {APPROVAL_STATUS_LABELS[approval.status]}
      {open ? ` · ${expiresIn(approval)}` : ""}
    </p>
    <h3>{approval.requested_action}</h3>
    <p className="notice">{approval.policy_reason}</p>
    <dl className="eval-expectations">
      <dt>Requested by</dt><dd><code>{approval.requester_subject}</code></dd>
      <dt>Run</dt>
      <dd><a href={`/workspaces/${workspaceId}/runs/${approval.run_id}`}>
        <code>{approval.run_id}</code></a></dd>
      <dt>Requested</dt>
      <dd><time dateTime={approval.created_at}>{toolTimestamp(approval.created_at)}</time></dd>
      <dt>{open ? "Expires" : "Decided"}</dt>
      <dd>{open
        ? <time dateTime={approval.expires_at}>{toolTimestamp(approval.expires_at)}</time>
        : <>
          <time dateTime={approval.decided_at!}>{toolTimestamp(approval.decided_at!)}</time>
          {approval.approver_subject && <> · <code>{approval.approver_subject}</code></>}
        </>}</dd>
    </dl>
    <details open={open}>
      <summary>Arguments the model proposed</summary>
      <pre className="eval-evidence">{formatArguments(approval.normalized_arguments)}</pre>
    </details>
    {open
      ? <div className="approval-actions">
        <p className="notice">
          Approving runs this exact call with these exact arguments. Nothing executes until you
          decide, and a changed tool contract invalidates the approval rather than reusing it.
        </p>
        <form action="/workspaces/approvals/decide" method="post">
          <input type="hidden" name="csrf" value={csrf}/>
          <input type="hidden" name="workspace" value={workspaceId}/>
          <input type="hidden" name="approval" value={approval.id}/>
          <button type="submit" name="decision" value="approved">Approve</button>
          <button type="submit" name="decision" value="rejected" className="reject">Reject</button>
        </form>
      </div>
      : <p className="notice">This request is closed. A new call creates a new approval.</p>}
  </article>;
}

export default async function Approvals({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string; status?: string; decided?: string; error?: string }>;
}) {
  const { id } = await params;
  const query = await searchParams;
  const session = await currentSession();
  if (!session) redirect("/login");

  const status = query.status ? approvalStatusSchema.safeParse(query.status) : null;
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !z.uuid().safeParse(query.cursor).success)
    || (status !== null && !status.success)) {
    return <InvalidLink back={`/workspaces/${id}`} backLabel="Back to workspace"/>;
  }

  const root = `/workspaces/${id}/approvals`;
  const selected = status?.success ? status.data : "pending";
  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    const approvals = await api(
      session,
      `/api/v1/workspaces/${id}/approvals?limit=25&status=${selected}`
        + (query.cursor ? "&cursor=" + query.cursor : ""),
      approvalPageSchema,
    );

    return <section className="workspace-content evaluation-content">
      <a href={`/workspaces/${id}`}>← {workspace.name}</a>
      <p className="eyebrow">GOVERNANCE / HUMAN DECISIONS</p><h1>Tool approvals</h1>
      <p className="intro">
        A governed tool call that policy holds for a human waits here. The run keeps its place and
        nothing is sent to the tool until someone decides.
      </p>
      <p className="notice">
        Destructive tools and tools that send external communication always reach this page, even
        when their stored policy says allow. An approval expires on its own if nobody decides.
      </p>
      <Saved message={query.decided === "approved" ? "The call was approved and its run was requeued."
        : query.decided === "rejected" ? "The call was rejected. The tool was not called." : null}/>
      <Failed message={query.error ? MESSAGES[query.error] ?? MESSAGES.failed : null}/>

      <nav className="spend-filters" aria-label="Filter approvals">
        {approvalStatusSchema.options.map(option => <a
          key={option}
          href={option === "pending" ? root : `${root}?status=${option}`}
          aria-current={selected === option ? "page" : undefined}
        >{APPROVAL_STATUS_LABELS[option]}</a>)}
      </nav>

      {approvals.items.length === 0
        ? <div className="empty">
          <h2>{selected === "pending" ? "Nothing is waiting for a decision" : "No approvals on this page"}</h2>
          <p>{selected === "pending"
            ? "Approvals appear here the moment a run reaches a tool that policy holds for a human."
            : "Closed approvals stay on record; nothing matches this filter yet."}</p></div>
        : <div className="eval-list">{approvals.items.map(approval => <Approval
          key={approval.id} approval={approval} workspaceId={id} csrf={session.csrf}
        />)}</div>}

      <nav className="pagination" aria-label="Approval pages">
        {query.cursor && <a href={selected === "pending" ? root : `${root}?status=${selected}`}>
          First page</a>}
        {approvals.next_cursor && <a
          href={`${root}?status=${selected}&cursor=${approvals.next_cursor}`}
        >More approvals →</a>}
      </nav>
      <p className="notice">
        Tool contracts and their policies are on the{" "}
        <a href={`/workspaces/${id}/tools`}>tools page</a>.
      </p>
    </section>;
  } catch (error) {
    return <ConsoleError
      error={error} area="Approvals" back={`/workspaces/${id}`} backLabel="Back to workspace"
    />;
  }
}
