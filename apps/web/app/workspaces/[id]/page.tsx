import { redirect } from "next/navigation";
import { z } from "zod";
import { currentSession } from "../../../lib/server/session";
import { api, ApiError } from "../../../lib/server/api";
import { workspaceSchema } from "../../../lib/workspace-contracts";
export default async function WorkspaceDetail({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ error?: string; saved?: string }> }) {
  const { id } = await params; const query = await searchParams;
  const session = await currentSession(); if (!session) redirect("/login");
  if (!z.uuid().safeParse(id).success) return <section><h1>Workspace not found</h1></section>;
  let workspace;
  try { workspace = await api(session, "/api/v1/workspaces/" + id, workspaceSchema); }
  catch (error) { if (error instanceof ApiError && error.status === 401) redirect("/login?error=session_expired");
    return <section><h1>{error instanceof ApiError && [403,404].includes(error.status) ? "Workspace not found or access denied" : "Workspace unavailable"}</h1><a href="/workspaces">Back to workspaces</a></section>; }
  return <section className="workspace-content"><a href="/workspaces">← All workspaces</a><h1>{workspace.name}</h1><p className="badge">Your role: {workspace.role}</p>
    <div className="grid console-links">{[
      { slug: "agents", name: "Agents", detail: "Define agents and start runs." },
      { slug: "runs", name: "Agent runs", detail: "Follow execution, events and results." },
      { slug: "approvals", name: "Tool approvals", detail: "Decide the calls policy holds for a human." },
      { slug: "tools", name: "Tools and policy", detail: "Review tool contracts and set their policy." },
      { slug: "knowledge", name: "Knowledge sources", detail: "Manage what agents may retrieve." },
      { slug: "evaluations", name: "Evaluation suites", detail: "Compare versioned quality results." },
      { slug: "spend", name: "Spend and budget", detail: "Track model cost and set the cap." },
    ].map(area => <article className="card" key={area.slug}>
      <h2><a href={`/workspaces/${id}/${area.slug}`}>{area.name} →</a></h2>
      <p>{area.detail}</p>
    </article>)}</div>
    {query.error && <p role="alert">The change could not be saved. Check your inputs and permissions.</p>}
    {query.saved && <p role="status">Your changes were saved.</p>}
    {workspace.role !== "member" && <form className="workspace-form" action="/workspaces/mutate" method="post"><h2>Workspace settings</h2><input type="hidden" name="csrf" value={session.csrf}/><input type="hidden" name="workspace" value={id}/><input type="hidden" name="operation" value="rename"/><label htmlFor="name">Workspace name</label><input id="name" name="name" defaultValue={workspace.name} required maxLength={100}/><button type="submit">Save name</button></form>}
    {workspace.role === "owner" && <form className="workspace-form" action="/workspaces/mutate" method="post"><h2>Team access</h2><p>Grant or update access using the account ID supplied by your organization. This does not send an invitation.</p><input type="hidden" name="csrf" value={session.csrf}/><input type="hidden" name="workspace" value={id}/><input type="hidden" name="operation" value="member"/><label htmlFor="subject">Account ID</label><input id="subject" name="subject" required maxLength={255}/><label htmlFor="role">Role</label><select id="role" name="role"><option value="member">Member · read only</option><option value="admin">Admin · edit workspace</option></select><button type="submit">Save access</button></form>}
    {workspace.role === "member" && <p className="notice">You have read-only access. Contact the workspace owner to request changes.</p>}
  </section>;
}
