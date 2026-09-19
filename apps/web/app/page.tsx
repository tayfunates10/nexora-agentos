import { fetchHealth } from "../lib/health";
export const dynamic = "force-dynamic";

export default async function Home() {
  const state = await fetchHealth(process.env.NEXORA_API_URL ?? "http://127.0.0.1:8000");
  const health = state.kind === "connected" ? state.health : null;
  const notice = state.kind === "forbidden" ? "Access to platform health is restricted."
    : state.kind === "invalid" ? "The API returned an unexpected response."
    : state.kind === "unavailable" ? "The API is unreachable. Check the API service and refresh."
    : health?.status === "degraded" ? "Some dependencies are unavailable. Agent execution is not enabled."
    : "All foundation services are reachable.";
  return <main>
    <header><a href="/" className="brand"><span className="mark">N</span> NEXORA <span className="muted">/ AgentOS</span></a><a className="badge" href="/workspaces">Open workspaces ↗</a></header>
    <section className="hero"><p className="eyebrow">PLATFORM OVERVIEW</p><h1>A clear view of your<br /><span>agent infrastructure.</span></h1><p className="intro">One control plane for the services behind your agents. Start with a healthy foundation, then build controlled execution.</p></section>
    <section aria-labelledby="health-title"><div className="section-head"><h2 id="health-title">Service health</h2><a href="/">Refresh status ↗</a></div><p className="notice" role="status">{notice}</p>
      <div className="grid">{[
        { name: "Control API", detail: "FastAPI · versioned contracts", up: health ? "up" : "unknown" },
        { name: "PostgreSQL", detail: "Persistent data · pgvector", up: health?.dependencies.postgres ?? "unknown" },
        { name: "Redis", detail: "Queue infrastructure", up: health?.dependencies.redis ?? "unknown" },
      ].map(service => <article className="card" key={service.name}><p className={`state ${service.up}`}>{service.up === "up" ? "Available" : service.up === "down" ? "Unavailable" : "Unknown"}</p><h3>{service.name}</h3><p>{service.detail}</p></article>)}</div>
    </section>
    <section className="next" aria-labelledby="next-title"><div><p className="eyebrow">WORKSPACE MANAGEMENT</p><h2 id="next-title">Workspaces & access control</h2><p>Create workspaces and manage team access with your organization account.</p></div><a className="badge" href="/workspaces">Open workspaces ↗</a></section>
    <section className="empty"><h2>No agent runs yet</h2><p>Agent runtime, tool approvals and MCP connections are planned. This foundation does not execute agents or call model providers.</p></section>
    <footer>Nexora AgentOS <span>Explicit boundaries. Observable execution.</span></footer>
  </main>;
}
