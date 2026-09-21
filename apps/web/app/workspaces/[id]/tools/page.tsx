import { redirect } from "next/navigation";
import { z } from "zod";
import { api } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import {
  POLICY_LABELS,
  SIDE_EFFECT_LABELS,
  forcesApproval,
  policyDecisionSchema,
  policyLabel,
  policyTone,
  toolPageSchema,
  toolTimestamp,
} from "../../../../lib/tool-contracts";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { ConsoleError, Failed, InvalidLink, Saved } from "../console";

const MESSAGES: Record<string, string> = {
  invalid: "Choose a decision and give a reason of up to 500 characters.",
  forbidden: "Only workspace owners and admins can change a tool policy.",
  failed: "The policy could not be saved. Check your current access and try again.",
};

export default async function Tools({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string; saved?: string; error?: string }>;
}) {
  const { id } = await params;
  const query = await searchParams;
  const session = await currentSession();
  if (!session) redirect("/login");
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !/^[a-z][a-z0-9_.-]{1,63}$/.test(query.cursor))) {
    return <InvalidLink back={`/workspaces/${id}`} backLabel="Back to workspace"/>;
  }

  const root = `/workspaces/${id}/tools`;
  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    const tools = await api(
      session,
      `/api/v1/workspaces/${id}/tools?limit=25${query.cursor ? "&cursor=" + query.cursor : ""}`,
      toolPageSchema,
    );
    const canManage = workspace.role !== "member";

    return <section className="workspace-content evaluation-content">
      <a href={`/workspaces/${id}`}>← {workspace.name}</a>
      <p className="eyebrow">GOVERNANCE / TOOL CONTRACTS</p><h1>Tools and policy</h1>
      <p className="intro">
        Every tool an agent may call is a typed contract with an explicit decision. A tool with no
        policy is denied: the platform never falls back to allowing a call.
      </p>
      <p className="notice">
        Contracts are registered through the API by whoever integrates the tool, because they
        carry a JSON schema and a server key. Workspaces never supply a URL, command or
        credential; the operator maps a server key to a real endpoint in the worker configuration.
      </p>
      <Saved message={query.saved === "policy" ? "The policy was saved." : null}/>
      <Failed message={query.error ? MESSAGES[query.error] ?? MESSAGES.failed : null}/>

      {tools.items.length === 0
        ? <div className="empty"><h2>No tools registered</h2>
          <p>Agents can run without tools. Register a contract through
            <code> PUT /api/v1/workspaces/{"{id}"}/tools/{"{name}"}</code> to govern one.</p></div>
        : <div className="eval-list">{tools.items.map(tool => <article className="card eval-card" key={tool.id}>
          <p className={"state " + policyTone(tool)}>{policyLabel(tool)}</p>
          <h2>{tool.name}</h2>
          <p>{tool.description}</p>
          <dl className="eval-expectations">
            <dt>Side effect</dt><dd>{SIDE_EFFECT_LABELS[tool.side_effect]}</dd>
            <dt>Server</dt><dd><code>{tool.server_key}</code> · <code>{tool.remote_name}</code></dd>
            <dt>Enabled</dt><dd>{tool.enabled ? "Yes" : "No · calls are refused"}</dd>
            {tool.policy_reason && <><dt>Policy reason</dt><dd>{tool.policy_reason}</dd></>}
            {tool.policy_updated_at && <><dt>Policy set</dt>
              <dd><time dateTime={tool.policy_updated_at}>
                {toolTimestamp(tool.policy_updated_at)}</time></dd></>}
          </dl>
          {forcesApproval(tool) && <p className="notice">
            Allow does not remove the human step for this tool: its side effect always raises an
            approval on the <a href={`/workspaces/${id}/approvals`}>approvals page</a>.
          </p>}
          <details>
            <summary>Input contract</summary>
            <pre className="eval-evidence">{JSON.stringify(tool.input_schema, null, 2)}</pre>
          </details>
          {canManage && <form className="workspace-form policy-form" action="/workspaces/tools/policy" method="post">
            <input type="hidden" name="csrf" value={session.csrf}/>
            <input type="hidden" name="workspace" value={id}/>
            <input type="hidden" name="tool" value={tool.name}/>
            <label htmlFor={`decision-${tool.id}`}>Policy decision</label>
            <select
              id={`decision-${tool.id}`} name="decision"
              defaultValue={tool.policy_decision ?? "deny"}
            >{policyDecisionSchema.options.map(option => <option key={option} value={option}>
              {POLICY_LABELS[option]}
            </option>)}</select>
            <label htmlFor={`reason-${tool.id}`}>Reason</label>
            <input
              id={`reason-${tool.id}`} name="reason" required maxLength={500} autoComplete="off"
              defaultValue={tool.policy_reason ?? ""} aria-describedby={`reason-help-${tool.id}`}
            />
            <p id={`reason-help-${tool.id}`} className="notice">
              The reason is recorded with your identity in the workspace audit trail and shown to
              whoever decides an approval for this tool.
            </p>
            <button type="submit">Save policy</button>
          </form>}
        </article>)}</div>}

      <nav className="pagination" aria-label="Tool pages">
        {query.cursor && <a href={root}>First page</a>}
        {tools.next_cursor && <a href={root + "?cursor=" + tools.next_cursor}>More tools →</a>}
      </nav>
      {!canManage && <p className="notice">
        You have read-only access. Only owners and admins can change a tool policy.
      </p>}
    </section>;
  } catch (error) {
    return <ConsoleError
      error={error} area="Tools" back={`/workspaces/${id}`} backLabel="Back to workspace"
    />;
  }
}
