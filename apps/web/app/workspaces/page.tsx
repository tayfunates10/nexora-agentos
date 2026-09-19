import { redirect } from "next/navigation";
import { z } from "zod";
import { currentSession } from "../../lib/server/session";
import { api, ApiError } from "../../lib/server/api";
import { pageSchema } from "../../lib/workspace-contracts";
export default async function Workspaces({ searchParams }: { searchParams: Promise<{ cursor?: string; error?: string }> }) {
  const session = await currentSession(); if (!session) redirect("/login");
  const query = await searchParams;
  const cursor = z.uuid().safeParse(query.cursor);
  if (query.cursor && !cursor.success) return <section><h1>Invalid page</h1><a href="/workspaces">Back to workspaces</a></section>;
  let page;
  try { page = await api(session, "/api/v1/workspaces" + (cursor.success ? "?cursor=" + cursor.data : ""), pageSchema); }
  catch (error) { if (error instanceof ApiError && error.status === 401) redirect("/login?error=session_expired");
    return <section><h1>Workspaces unavailable</h1><p>We could not load your workspaces. Please try again.</p><a href="/workspaces">Retry</a></section>; }
  return <section className="workspace-content"><p className="eyebrow">CONTROL PLANE</p><h1>Your workspaces.</h1><p className="intro">Manage the spaces you belong to. Access is checked for every operation.</p>
    {query.error && <p role="alert" className="notice">The change could not be saved. Check your inputs and permissions, then try again.</p>}
    <form className="workspace-form" action="/workspaces/mutate" method="post"><h2>Create a workspace</h2><input type="hidden" name="csrf" value={session.csrf}/><input type="hidden" name="operation" value="create"/><label htmlFor="name">Workspace name</label><input id="name" name="name" required maxLength={100} placeholder="e.g. Product engineering"/><button type="submit">Create workspace</button></form>
    {page.items.length === 0 ? <div className="empty"><h2>No workspaces yet</h2><p>Create your first workspace or ask its owner to add your account.</p></div>
      : <div className="grid">{page.items.map(workspace => <a key={workspace.id} className="card" href={"/workspaces/" + workspace.id}><span className="badge">{workspace.role}</span><h2>{workspace.name}</h2><p>Manage workspace ↗</p></a>)}</div>}
    <nav className="pagination" aria-label="Workspace pages">{query.cursor && <a href="/workspaces">First page</a>}{page.next_cursor && <a href={"/workspaces?cursor=" + page.next_cursor}>Next page →</a>}</nav>
  </section>;
}
