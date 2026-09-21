import { randomUUID } from "node:crypto";
import { redirect } from "next/navigation";
import { z } from "zod";
import {
  INGESTION_STATUS_HELP,
  INGESTION_STATUS_LABELS,
  INGESTION_STATUS_TONES,
  MAX_SOURCE_CHARS,
  SCOPE_LABELS,
  ingestionSchema,
  knowledgeSourcePageSchema,
  knowledgeTimestamp,
} from "../../../../lib/knowledge-contracts";
import { api, ApiError } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { ConsoleError, Failed, InvalidLink, Saved } from "../console";

const MESSAGES: Record<string, string> = {
  invalid: "Check the source key, version, title and text. A key may contain letters, digits and . _ : - only.",
  forbidden: "Only workspace owners and admins can add or remove knowledge sources.",
  busy: "That source is being ingested right now. Wait for the job to finish, then try again.",
  failed: "The request could not be completed. Check your current access and try again.",
};

export default async function Knowledge({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string; job?: string; saved?: string; error?: string }>;
}) {
  const { id } = await params;
  const query = await searchParams;
  const session = await currentSession();
  if (!session) redirect("/login");
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !z.uuid().safeParse(query.cursor).success)
    || (query.job !== undefined && !z.uuid().safeParse(query.job).success)) {
    return <InvalidLink back={`/workspaces/${id}`} backLabel="Back to workspace"/>;
  }

  const root = `/workspaces/${id}/knowledge`;
  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    const sources = await api(
      session,
      `/api/v1/workspaces/${id}/knowledge/sources?limit=25`
        + (query.cursor ? "&cursor=" + query.cursor : ""),
      knowledgeSourcePageSchema,
    );
    // A job the console just queued is followed by identifier; a stale one is simply gone.
    let job = null;
    if (query.job) {
      try {
        job = await api(
          session, `/api/v1/workspaces/${id}/knowledge/ingestions/${query.job}`, ingestionSchema,
        );
      } catch (error) {
        if (!(error instanceof ApiError) || ![403, 404].includes(error.status)) throw error;
      }
    }
    const canManage = workspace.role !== "member";

    return <section className="workspace-content evaluation-content">
      <a href={`/workspaces/${id}`}>← {workspace.name}</a>
      <p className="eyebrow">KNOWLEDGE / RETRIEVAL</p><h1>Knowledge sources</h1>
      <p className="intro">
        Text an agent may retrieve, with the access scope that decides who it can be retrieved
        for. Sources are versioned: ingesting the same key again indexes a new version.
      </p>
      <p className="notice">
        The API stores the text and queues the work. Embedding happens in a retrieval-enabled
        worker, so this process never calls a model provider, and the embedding cost is metered
        against this workspace&apos;s budget. Retrieval applies each source&apos;s scope at query time.
      </p>
      <Saved message={query.saved === "queued" ? "The source was queued for ingestion." : null}/>
      <Saved message={query.saved === "deleted" ? "The source was deleted. Queued ingestion for the same key was cancelled." : null}/>
      <Failed message={query.error ? MESSAGES[query.error] ?? MESSAGES.failed : null}/>

      {job && <div className="card run-panel">
        <p className={"state " + INGESTION_STATUS_TONES[job.status]}>
          {INGESTION_STATUS_LABELS[job.status]}
        </p>
        <h2>{job.title}</h2>
        <p className="notice">{INGESTION_STATUS_HELP[job.status]}</p>
        <dl className="eval-expectations">
          <dt>Source</dt><dd><code>{job.source_key}</code> · version <code>{job.version}</code></dd>
          <dt>Attempts</dt><dd>{job.attempt_count}</dd>
          {job.chunk_count !== null && <><dt>Chunks</dt><dd>{job.chunk_count}</dd></>}
          {job.embedding_input_tokens !== null && <><dt>Embedding tokens</dt>
            <dd>{job.embedding_input_tokens.toLocaleString("en-GB")}</dd></>}
          {job.error_code && <><dt>Error</dt><dd><code>{job.error_code}</code></dd></>}
        </dl>
        <div className="section-head">
          <p className="notice">Queued work updates when this page is reloaded.</p>
          <a href={`${root}?job=${job.id}`}>Refresh ↗</a>
        </div>
      </div>}

      {sources.items.length === 0
        ? <div className="empty"><h2>No indexed sources on this page</h2>
          <p>{canManage
            ? "Add a source below. It becomes retrievable once a retrieval-enabled worker has indexed it."
            : "Ask a workspace owner or admin to add the documents this workspace should retrieve."}</p></div>
        : <table className="spend-table">
          <caption>Indexed source versions. Deleting a key removes its indexed content.</caption>
          <thead><tr>
            <th scope="col">Title</th><th scope="col">Key</th><th scope="col">Version</th>
            <th scope="col">Chunks</th><th scope="col">Access</th><th scope="col">Indexed</th>
            {canManage && <th scope="col">Remove</th>}
          </tr></thead>
          <tbody>{sources.items.map(source => <tr key={source.id}>
            <td data-label="Title">{source.title}</td>
            <td data-label="Key"><code>{source.source_key}</code></td>
            <td data-label="Version"><code>{source.version}</code></td>
            <td data-label="Chunks">{source.chunk_count}</td>
            <td data-label="Access">{SCOPE_LABELS[source.access_scope]}</td>
            <td data-label="Indexed">
              <time dateTime={source.updated_at}>{knowledgeTimestamp(source.updated_at)}</time></td>
            {canManage && <td data-label="Remove">
              <form action="/workspaces/knowledge/delete" method="post">
                <input type="hidden" name="csrf" value={session.csrf}/>
                <input type="hidden" name="workspace" value={id}/>
                <input type="hidden" name="source" value={source.source_key}/>
                <button type="submit" className="reject">Delete</button>
              </form></td>}
          </tr>)}</tbody>
        </table>}

      <nav className="pagination" aria-label="Knowledge source pages">
        {query.cursor && <a href={root}>First page</a>}
        {sources.next_cursor && <a href={root + "?cursor=" + sources.next_cursor}>More sources →</a>}
      </nav>

      <h2 className="eval-section-title">Add a source</h2>
      {canManage
        ? <form className="workspace-form" action="/workspaces/knowledge/create" method="post">
          <p>
            UTF-8 text or Markdown, up to {MAX_SOURCE_CHARS.toLocaleString("en-GB")} characters.
            PDFs and other binary formats are not accepted here: they need a parser boundary this
            platform does not have yet.
          </p>
          <input type="hidden" name="csrf" value={session.csrf}/>
          <input type="hidden" name="workspace" value={id}/>
          {/* Minted per render so a resubmitted form replays one ingestion, not two. */}
          <input type="hidden" name="idempotency" value={randomUUID()}/>
          <label htmlFor="title">Title</label>
          <input id="title" name="title" required maxLength={500} autoComplete="off"/>
          <label htmlFor="source_key">Source key</label>
          <input
            id="source_key" name="source_key" required maxLength={255} autoComplete="off"
            pattern="[A-Za-z0-9._:\-]+" aria-describedby="key-help"
          />
          <p id="key-help" className="notice">
            A stable identifier such as <code>handbook</code>. Citations name this key and its
            version, so keep it stable across updates.
          </p>
          <label htmlFor="version">Version</label>
          <input
            id="version" name="version" required maxLength={128} defaultValue="v1"
            autoComplete="off"
          />
          <label htmlFor="access_scope">Who may retrieve it</label>
          <select id="access_scope" name="access_scope" defaultValue="workspace">
            <option value="workspace">{SCOPE_LABELS.workspace}</option>
          </select>
          <p className="notice">
            Restricted sources are limited to named accounts and are set through the knowledge API,
            which takes the exact issuer and subject of each account allowed to retrieve them.
          </p>
          <label htmlFor="text">Content</label>
          <textarea id="text" name="text" required rows={10} maxLength={MAX_SOURCE_CHARS}/>
          <button type="submit">Queue ingestion</button>
        </form>
        : <p className="notice">
          You have read-only access to knowledge sources. Agents still retrieve from them on your
          behalf, subject to each source&apos;s access scope.
        </p>}
    </section>;
  } catch (error) {
    return <ConsoleError
      error={error} area="Knowledge" back={`/workspaces/${id}`} backLabel="Back to workspace"
    />;
  }
}
