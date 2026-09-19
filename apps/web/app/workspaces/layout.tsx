import { redirect } from "next/navigation";
import { currentSession } from "../../lib/server/session";
export const dynamic = "force-dynamic";
export default async function WorkspaceLayout({ children }: { children: React.ReactNode }) {
  let session;
  try { session = await currentSession(); } catch { return <main><h1>Session service unavailable</h1><p>Please try again shortly.</p><a href="/">Back to platform health</a></main>; }
  if (!session) redirect("/login");
  return <main><header><a className="brand" href="/workspaces">NEXORA / Workspaces</a><form method="post" action="/auth/logout"><input type="hidden" name="csrf" value={session.csrf}/><button type="submit">Sign out</button></form></header>{children}</main>;
}
