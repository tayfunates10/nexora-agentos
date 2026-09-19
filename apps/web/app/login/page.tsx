import { readConfig } from "../../lib/auth-core";
export const dynamic = "force-dynamic";
export default async function Login({ searchParams }: { searchParams: Promise<{ error?: string; status?: string }> }) {
  const query = await searchParams;
  let configured = false; try { configured = !!readConfig(); } catch {}
  return <main><header><a className="brand" href="/">NEXORA / AgentOS</a><a href="/">Platform health</a></header>
    <section className="login-panel"><p className="eyebrow">YOUR WORKSPACE</p><h1>Welcome back.</h1><p className="intro">Sign in with your organization account to manage your workspaces and team access.</p>
      {query.error && <p className="notice" role="alert">Sign-in could not be completed. Please start again.</p>}
      {query.status === "signed_out" && <p role="status">You are signed out of Nexora. Your identity-provider session may still be active.</p>}
      {configured ? <form action="/auth/login" method="post"><button type="submit">Continue to sign in ↗</button></form>
        : <p className="notice" role="status">Sign-in is not available yet. Ask your administrator to connect your organization account.</p>}
      <p className="muted">Your account is managed by your organization.</p>
    </section></main>;
}
