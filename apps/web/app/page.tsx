import { fetchHealth } from "../lib/health";
export const dynamic = "force-dynamic";

// What the signed-in console actually offers. Each entry is a real page behind sign-in;
// this landing page never claims a capability the platform does not have.
const CAPABILITIES = [
  {
    title: "Workspaces and access",
    detail: "Tenant-isolated workspaces with owner, admin and member roles, checked by the API on every request.",
  },
  {
    title: "Agents and durable runs",
    detail: "Named agents on operator-approved model profiles. Every run is queued in PostgreSQL with an append-only event history.",
  },
  {
    title: "Governed tools and approvals",
    detail: "Typed tool contracts with default-deny policy. Destructive and outbound calls wait for a human decision.",
  },
  {
    title: "Knowledge and retrieval",
    detail: "Versioned sources with access scopes, embedded in the worker and cited with provenance.",
  },
  {
    title: "Evaluations and quality",
    detail: "Versioned golden suites with baseline comparison, plus an optional pinned judge for probabilistic quality.",
  },
  {
    title: "Spend and budgets",
    detail: "Every priced provider call in an append-only ledger, with monthly limits enforced before egress.",
  },
];

export default async function Home() {
  const state = await fetchHealth(process.env.NEXORA_API_URL ?? "http://127.0.0.1:8000");
  const health = state.kind === "connected" ? state.health : null;
  const notice = state.kind === "forbidden" ? "Access to platform health is restricted."
    : state.kind === "invalid" ? "The API returned an unexpected response."
    : state.kind === "unavailable" ? "The API is unreachable. Check the API service and refresh."
    : health?.status === "degraded" ? "Some dependencies are unavailable. Agent execution is not enabled."
    : "All platform services are reachable.";
  return <main>
    <header><a href="/" className="brand"><span className="mark">N</span> NEXORA <span className="muted">/ AgentOS</span></a><a className="badge" href="/workspaces">Open workspaces ↗</a></header>
    <section className="hero"><p className="eyebrow">PLATFORM OVERVIEW</p><h1>Agents your organisation<br /><span>can actually audit.</span></h1><p className="intro">One control plane for agent execution: tenant isolation, governed tools with human approval, permission-aware retrieval, evaluations, metered spend and end-to-end traces.</p></section>
    <section aria-labelledby="health-title"><div className="section-head"><h2 id="health-title">Service health</h2><a href="/">Refresh status ↗</a></div><p className="notice" role="status">{notice}</p>
      <div className="grid">{[
        { name: "Control API", detail: "FastAPI · versioned contracts", up: health ? "up" : "unknown" },
        { name: "PostgreSQL", detail: "Runs, governance, ledger · pgvector", up: health?.dependencies.postgres ?? "unknown" },
        { name: "Redis", detail: "Job queue · browser sessions", up: health?.dependencies.redis ?? "unknown" },
      ].map(service => <article className="card" key={service.name}><p className={`state ${service.up}`}>{service.up === "up" ? "Available" : service.up === "down" ? "Unavailable" : "Unknown"}</p><h3>{service.name}</h3><p>{service.detail}</p></article>)}</div>
    </section>
    <section aria-labelledby="capability-title"><div className="section-head"><h2 id="capability-title">In the console</h2><a href="/workspaces">Sign in to a workspace ↗</a></div>
      <div className="grid">{CAPABILITIES.map(capability => <article className="card" key={capability.title}><h3>{capability.title}</h3><p>{capability.detail}</p></article>)}</div>
    </section>
    <section className="next" aria-labelledby="next-title"><div><p className="eyebrow">OPERATOR CONTROL</p><h2 id="next-title">Nothing runs unless you enable it</h2><p>Agent execution needs a worker started with an operator-managed model profile. Without one, runs stay queued and no model provider is ever called. Tools, retrieval, judging and alert delivery are each opt-in the same way.</p></div><a className="badge" href="/workspaces">Open workspaces ↗</a></section>
    <footer>Nexora AgentOS <span>Explicit boundaries. Observable execution.</span></footer>
  </main>;
}
