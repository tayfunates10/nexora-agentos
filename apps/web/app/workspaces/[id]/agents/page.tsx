import { randomUUID } from "node:crypto";
import { redirect } from "next/navigation";
import { z } from "zod";
import { agentPageSchema, runTimestamp } from "../../../../lib/agent-contracts";
import { api } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { ConsoleError, Failed, InvalidLink, Saved } from "../console";

const MESSAGES: Record<string, string> = {
  invalid: "Check the agent name, instructions and model profile, then try again.",
  forbidden: "You do not have the access this action needs in this workspace.",
  failed: "The request could not be completed. Check your current access and try again.",
};

export default async function Agents({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string; saved?: string; error?: string }>;
}) {
  const { id } = await params;
  const query = await searchParams;
  const session = await currentSession();
  if (!session) redirect("/login");
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !z.uuid().safeParse(query.cursor).success)) {
    return <InvalidLink back="/workspaces" backLabel="Back to workspaces"/>;
  }

  const root = `/workspaces/${id}/agents`;
  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    const agents = await api(
      session,
      `/api/v1/workspaces/${id}/agents?limit=25${query.cursor ? "&cursor=" + query.cursor : ""}`,
      agentPageSchema,
    );
    const canManage = workspace.role !== "member";

    return <section className="workspace-content evaluation-content">
      <a href={`/workspaces/${id}`}>← {workspace.name}</a>
      <p className="eyebrow">AGENTS / DEFINITIONS</p><h1>Agents</h1>
      <p className="intro">
        An agent is a named instruction set bound to an operator-approved model profile.
        Any workspace member can start a run; only owners and admins can change what an agent is.
      </p>
      <p className="notice">
        A run is queued in PostgreSQL and executed by the agent worker. If no worker is running
        for this deployment, runs stay queued and no model provider is ever called.
      </p>
      <Saved message={query.saved === "agent" ? "The agent was created." : null}/>
      <Failed message={query.error ? MESSAGES[query.error] ?? MESSAGES.failed : null}/>

      {agents.items.length === 0
        ? <div className="empty"><h2>No agents on this page</h2>
          <p>{canManage
            ? "Create the first agent below. Its model profile must be one this deployment's worker allows."
            : "Ask a workspace owner or admin to create an agent before starting a run."}</p></div>
        : <div className="eval-list">{agents.items.map(agent => <article className="card eval-card" key={agent.id}>
          <p className="eyebrow">PROFILE {agent.model_profile.toUpperCase()}</p>
          <h2>{agent.name}</h2>
          <p className="agent-instructions">{agent.instructions}</p>
          <p><time dateTime={agent.created_at}>{runTimestamp(agent.created_at)}</time></p>
          <details className="run-starter">
            <summary>Start a run with {agent.name}</summary>
            <form className="workspace-form" action="/workspaces/runs/start" method="post">
              <input type="hidden" name="csrf" value={session.csrf}/>
              <input type="hidden" name="workspace" value={id}/>
              <input type="hidden" name="agent" value={agent.id}/>
              {/* Minted per render: submitting the same form twice replays one idempotency
                  key, so a double click returns the existing run instead of starting another. */}
              <input type="hidden" name="idempotency" value={randomUUID()}/>
              <label htmlFor={`input-${agent.id}`}>What should this agent do?</label>
              <textarea
                id={`input-${agent.id}`} name="input" required rows={4} maxLength={20000}
                aria-describedby={`input-help-${agent.id}`}
              />
              <p id={`input-help-${agent.id}`} className="notice">
                Treated as untrusted input by the runtime. Tool calls still pass workspace policy
                and, where required, human approval.
              </p>
              <button type="submit">Start run</button>
            </form>
          </details>
        </article>)}</div>}

      <nav className="pagination" aria-label="Agent pages">
        {query.cursor && <a href={root}>First page</a>}
        {agents.next_cursor && <a href={root + "?cursor=" + agents.next_cursor}>More agents →</a>}
      </nav>

      <h2 className="eval-section-title">Create an agent</h2>
      {canManage
        ? <form className="workspace-form" action="/workspaces/agents/create" method="post">
          <input type="hidden" name="csrf" value={session.csrf}/>
          <input type="hidden" name="workspace" value={id}/>
          <label htmlFor="name">Agent name</label>
          <input id="name" name="name" required maxLength={100} autoComplete="off"/>
          <label htmlFor="instructions">Instructions</label>
          <textarea id="instructions" name="instructions" required rows={6} maxLength={20000}/>
          <label htmlFor="model_profile">Model profile</label>
          <input
            id="model_profile" name="model_profile" required maxLength={64} defaultValue="default"
            pattern="[A-Za-z0-9._\-]+" autoComplete="off" aria-describedby="profile-help"
          />
          <p id="profile-help" className="notice">
            The profile decides the provider, model and tools this agent may use. A run whose
            workspace is not listed in that profile fails without reaching a provider.
          </p>
          <button type="submit">Create agent</button>
        </form>
        : <p className="notice">
          You have read-only access to agent definitions. You can still start runs with the
          agents listed above.
        </p>}
    </section>;
  } catch (error) {
    return <ConsoleError
      error={error} area="Agents" back={`/workspaces/${id}`} backLabel="Back to workspace"
    />;
  }
}
