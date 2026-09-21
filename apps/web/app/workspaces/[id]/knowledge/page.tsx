import { randomUUID } from "node:crypto";
import { redirect } from "next/navigation";
import { z } from "zod";
import {
  INGESTION_STATUS_TONES, MAX_SOURCE_CHARS, ingestionHelpKey, ingestionSchema,
  ingestionStatusKey, knowledgeSourcePageSchema, scopeKey,
} from "../../../../lib/knowledge-contracts";
import { api, ApiError } from "../../../../lib/server/api";
import { currentSession } from "../../../../lib/server/session";
import { consoleChrome } from "../../../../lib/server/chrome";
import { workspaceSchema } from "../../../../lib/workspace-contracts";
import { formatNumber, formatTimestamp } from "../../../../lib/i18n/format.ts";
import { ConsoleBreadcrumb, ConsoleShell } from "../../../../components/shell/ConsoleShell.tsx";
import {
  Detail, DetailList, EmptyState, Field, Hint, Notice, PageHeader, Pagination, Panel,
  RefreshLink, StatusBadge,
} from "../../../../components/ui/primitives.tsx";
import { Reveal } from "../../../../components/ui/Reveal.tsx";
import { ConsoleProblem, Failed, InvalidLinkPage, Saved } from "../console";
import type { MessageKey } from "../../../../messages/en.ts";

const MESSAGES: Record<string, MessageKey> = {
  invalid: "knowledge.error.invalid",
  forbidden: "knowledge.error.forbidden",
  busy: "knowledge.error.busy",
  failed: "knowledge.error.failed",
};

export default async function Knowledge({ params, searchParams }: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ cursor?: string; job?: string; saved?: string; error?: string }>;
}) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  const session = await currentSession();
  if (!session) redirect("/login");

  const root = `/workspaces/${id}/knowledge`;
  let chrome = await consoleChrome(session);
  if (!z.uuid().safeParse(id).success
    || (query.cursor !== undefined && !z.uuid().safeParse(query.cursor).success)
    || (query.job !== undefined && !z.uuid().safeParse(query.job).success)) {
    return <InvalidLinkPage chrome={chrome} back={{
      href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace"),
    }}/>;
  }

  try {
    const workspace = await api(session, `/api/v1/workspaces/${id}`, workspaceSchema);
    chrome = await consoleChrome(session, workspace);
    const { ui } = chrome;
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

    return <ConsoleShell
      chrome={chrome}
      active="knowledge"
      breadcrumb={<ConsoleBreadcrumb
        ui={ui} workspace={workspace} trail={[{ href: root, label: ui.t("navigation.knowledge") }]}
      />}
    >
      <PageHeader
        eyebrow={ui.t("knowledge.eyebrow")}
        title={ui.t("knowledge.title")}
        intro={ui.t("knowledge.intro")}
      />
      <Hint>{ui.t("knowledge.workerNotice")}</Hint>
      <Saved message={query.saved === "queued" ? ui.t("knowledge.queued") : null}/>
      <Saved message={query.saved === "deleted" ? ui.t("knowledge.deleted") : null}/>
      <Failed message={query.error ? ui.t(MESSAGES[query.error] ?? MESSAGES.failed) : null}/>

      {job && <Panel labelledBy="ingestion-title" testId="ingestion-job">
        <StatusBadge tone={INGESTION_STATUS_TONES[job.status]}>
          {ui.t(ingestionStatusKey(job.status))}
        </StatusBadge>
        <h2 id="ingestion-title" className="section-title">{job.title}</h2>
        <p className="notice">{ui.t(ingestionHelpKey(job.status))}</p>
        <DetailList>
          <Detail label={ui.t("knowledge.job.source")}>
            <code>{job.source_key}</code> · <code>{job.version}</code></Detail>
          <Detail label={ui.t("knowledge.job.attempts")}>
            {formatNumber(job.attempt_count, ui.locale)}</Detail>
          {job.chunk_count !== null && <Detail label={ui.t("knowledge.job.chunks")}>
            {formatNumber(job.chunk_count, ui.locale)}</Detail>}
          {job.embedding_input_tokens !== null
            && <Detail label={ui.t("knowledge.job.embeddingTokens")}>
              {formatNumber(job.embedding_input_tokens, ui.locale)}</Detail>}
          {job.error_code && <Detail label={ui.t("knowledge.job.error")}>
            <code>{job.error_code}</code></Detail>}
        </DetailList>
        <div className="section-head">
          <p className="notice">{ui.t("knowledge.job.refreshNotice")}</p>
          <RefreshLink href={`${root}?job=${job.id}`} label={ui.t("common.refresh")}/>
        </div>
      </Panel>}

      {sources.items.length === 0
        ? <EmptyState title={ui.t("knowledge.emptyTitle")}>
          <p>{ui.t(canManage ? "knowledge.emptyManage" : "knowledge.emptyMember")}</p>
        </EmptyState>
        : <div className="table-scroll"><table className="table">
          <caption>{ui.t("knowledge.tableCaption")}</caption>
          <thead><tr>
            <th scope="col">{ui.t("knowledge.column.title")}</th>
            <th scope="col">{ui.t("knowledge.column.key")}</th>
            <th scope="col">{ui.t("knowledge.column.version")}</th>
            <th scope="col">{ui.t("knowledge.column.chunks")}</th>
            <th scope="col">{ui.t("knowledge.column.access")}</th>
            <th scope="col">{ui.t("knowledge.column.indexed")}</th>
            {canManage && <th scope="col">{ui.t("knowledge.column.remove")}</th>}
          </tr></thead>
          <tbody>{sources.items.map(source => <tr key={source.id}>
            {/* Title, key and version are the operator's own content, shown as stored. */}
            <td data-label={ui.t("knowledge.column.title")}>{source.title}</td>
            <td data-label={ui.t("knowledge.column.key")}><code>{source.source_key}</code></td>
            <td data-label={ui.t("knowledge.column.version")}><code>{source.version}</code></td>
            <td data-label={ui.t("knowledge.column.chunks")}>
              {formatNumber(source.chunk_count, ui.locale)}</td>
            <td data-label={ui.t("knowledge.column.access")}>
              {ui.t(scopeKey(source.access_scope))}</td>
            <td data-label={ui.t("knowledge.column.indexed")}>
              <time dateTime={source.updated_at}>
                {formatTimestamp(source.updated_at, ui.locale)}</time></td>
            {canManage && <td data-label={ui.t("knowledge.column.remove")}>
              <form action="/workspaces/knowledge/delete" method="post">
                <input type="hidden" name="csrf" value={session.csrf}/>
                <input type="hidden" name="workspace" value={id}/>
                <input type="hidden" name="source" value={source.source_key}/>
                <button type="submit" className="button danger small">{ui.t("common.delete")}</button>
              </form></td>}
          </tr>)}</tbody>
        </table></div>}

      <Pagination
        label={ui.t("knowledge.pagesLabel")}
        previous={query.cursor ? { href: root, label: ui.t("common.firstPage") } : null}
        next={sources.next_cursor
          ? { href: `${root}?cursor=${sources.next_cursor}`, label: ui.t("knowledge.more") }
          : null}
      />

      <Reveal>
        <Panel labelledBy="add-source-title">
          <h2 id="add-source-title" className="section-title">{ui.t("knowledge.addTitle")}</h2>
          {canManage
            ? <form className="form" action="/workspaces/knowledge/create" method="post">
              <Hint>{ui.t("knowledge.formatNotice", {
                max: formatNumber(MAX_SOURCE_CHARS, ui.locale),
              })}</Hint>
              <input type="hidden" name="csrf" value={session.csrf}/>
              <input type="hidden" name="workspace" value={id}/>
              {/* Minted per render so a resubmitted form replays one ingestion, not two. */}
              <input type="hidden" name="idempotency" value={randomUUID()}/>
              <Field id="title" label={ui.t("knowledge.titleLabel")}>
                <input
                  className="field-control" id="title" name="title" required maxLength={500}
                  autoComplete="off"
                />
              </Field>
              <div className="form-row">
                <Field
                  id="source_key" label={ui.t("knowledge.keyLabel")} help={ui.t("knowledge.keyHelp")}
                >
                  <input
                    className="field-control" id="source_key" name="source_key" required
                    maxLength={255} autoComplete="off" pattern="[A-Za-z0-9._:\-]+"
                    aria-describedby="source_key-help"
                  />
                </Field>
                <Field id="version" label={ui.t("knowledge.versionLabel")}>
                  <input
                    className="field-control" id="version" name="version" required maxLength={128}
                    defaultValue="v1" autoComplete="off"
                  />
                </Field>
              </div>
              <Field id="access_scope" label={ui.t("knowledge.accessLabel")}>
                <select
                  className="field-control" id="access_scope" name="access_scope"
                  defaultValue="workspace"
                >
                  <option value="workspace">{ui.t("knowledge.scope.workspace")}</option>
                </select>
              </Field>
              <Hint>{ui.t("knowledge.restrictedNotice")}</Hint>
              <Field id="text" label={ui.t("knowledge.contentLabel")}>
                <textarea
                  className="field-control" id="text" name="text" required rows={10}
                  maxLength={MAX_SOURCE_CHARS}
                />
              </Field>
              <div><button type="submit" className="button">{ui.t("knowledge.submit")}</button></div>
            </form>
            : <Hint>{ui.t("knowledge.readOnlyNotice")}</Hint>}
        </Panel>
      </Reveal>
    </ConsoleShell>;
  } catch (error) {
    return <ConsoleProblem
      chrome={chrome} error={error} area={chrome.ui.t("knowledge.area")} active="knowledge"
      back={{ href: `/workspaces/${id}`, label: chrome.ui.t("errors.backToWorkspace") }}
    />;
  }
}
