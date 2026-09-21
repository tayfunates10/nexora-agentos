// Test-only OIDC issuer and API fixture. Never imported by application routes.
import { createServer } from "node:http";
import { createHash, randomUUID } from "node:crypto";
import { generateKeyPair, exportJWK, SignJWT, jwtVerify } from "jose";
import type { AddressInfo } from "node:net";
import type { Agent, RunEvent, RunResult } from "../../lib/agent-contracts.ts";
import type { Ingestion, KnowledgeSource } from "../../lib/knowledge-contracts.ts";
import type { Tool, ToolApproval } from "../../lib/tool-contracts.ts";
import type { EvalJudgeRun, EvalRun, EvalSuite } from "../../lib/evaluation-contracts.ts";
import type { SpendRecord } from "../../lib/spend-contracts.ts";

export async function startProvider() {
  const { privateKey, publicKey } = await generateKeyPair("RS256");
  const jwk = { ...await exportJWK(publicKey), kid: "fixture-key", alg: "RS256", use: "sig" };
  const codes = new Map<string, { nonce: string; challenge: string; redirect: string }>();
  const workspaces = new Map<string, { id: string; name: string; role: string }>();
  const agents = new Map<string, Agent>();
  // The fixture has one signed-in identity, so requested_by_me is stored per run and a test
  // can flip it to exercise the requester-only result boundary.
  const runs = new Map<string, {
    id: string; workspace_id: string; agent_id: string; agent_name: string; trace_id: string;
    status: string; attempt_count: number; requested_by_me: boolean;
    cancel_requested_at: string | null; finished_at: string | null; failure_code: string | null;
    created_at: string; updated_at: string;
  }>();
  const runEvents = new Map<string, RunEvent[]>();
  const runResults = new Map<string, RunResult>();
  const runIdempotency = new Map<string, string>();
  const sources = new Map<string, KnowledgeSource & { workspace_id: string }>();
  const ingestions = new Map<string, Ingestion>();
  const ingestionIdempotency = new Map<string, string>();
  const tools = new Map<string, Tool>();
  const approvals = new Map<string, ToolApproval>();
  const evalSuites = new Map<string, EvalSuite>();
  const evalRuns = new Map<string, EvalRun>();
  const evalJudgeRuns = new Map<string, EvalJudgeRun>();
  const judgeIdempotency = new Map<string, string>();
  const spendRecords = new Map<string, SpendRecord & { workspace_id: string }>();
  const budgets = new Map<string, {
    monthly_limit_micros: number; enforcement: string; alert_thresholds: number[];
  }>();
  const spendAlerts = new Map<string, {
    threshold_percent: number; monthly_limit_micros: number;
    consumed_micros: number; enforcement: string; created_at: string;
  }>();
  let historyUnavailable = false;
  let spendUnavailable = false;
  let issuer = "";
  let wrongNonce = false;
  // Mirrors the API: a threshold is recorded once per workspace per period.
  const raiseAlerts = (workspaceId: string) => {
    const budget = budgets.get(workspaceId);
    if (!budget) return;
    const consumed = [...spendRecords.values()]
      .filter(row => row.workspace_id === workspaceId)
      .reduce((total, row) => total + row.cost_micros, 0);
    for (const percent of budget.alert_thresholds) {
      const key = `${workspaceId}:${percent}`;
      if (spendAlerts.has(key)) continue;
      if (consumed * 100 < percent * budget.monthly_limit_micros) continue;
      spendAlerts.set(key, {
        threshold_percent: percent,
        monthly_limit_micros: budget.monthly_limit_micros,
        consumed_micros: consumed,
        enforcement: budget.enforcement,
        created_at: "2026-09-20T15:30:00Z",
      });
    }
  };
  const server = createServer(async (request, response) => {
    const send = (body: unknown, status = 200) => { response.writeHead(status, { "Content-Type": "application/json" }); response.end(JSON.stringify(body)); };
    try {
      const url = new URL(request.url!, issuer);
      if (url.pathname === "/.well-known/openid-configuration") return send({ issuer, authorization_endpoint: issuer + "authorize", token_endpoint: issuer + "token", jwks_uri: issuer + "jwks", response_types_supported: ["code"], subject_types_supported: ["public"], id_token_signing_alg_values_supported: ["RS256"], token_endpoint_auth_methods_supported: ["client_secret_post"], code_challenge_methods_supported: ["S256"] });
      if (url.pathname === "/jwks") return send({ keys: [jwk] });
      if (url.pathname === "/authorize") {
        const code = randomUUID();
        codes.set(code, { nonce: url.searchParams.get("nonce")!, challenge: url.searchParams.get("code_challenge")!, redirect: url.searchParams.get("redirect_uri")! });
        const target = new URL(url.searchParams.get("redirect_uri")!); target.searchParams.set("code", code); target.searchParams.set("state", url.searchParams.get("state")!);
        response.writeHead(302, { Location: target.href }); response.end(); return;
      }
      let body = ""; for await (const chunk of request) body += chunk;
      if (url.pathname === "/token") {
        const form = new URLSearchParams(body); const code = codes.get(form.get("code")!); codes.delete(form.get("code")!);
        if (!code || form.get("client_secret") !== "fixture-secret" || form.get("client_id") !== "fixture-client" || form.get("redirect_uri") !== code.redirect || createHash("sha256").update(form.get("code_verifier") ?? "").digest("base64url") !== code.challenge) return send({ error: "invalid_grant" }, 400);
        const idToken = await new SignJWT({ nonce: wrongNonce ? "wrong" : code.nonce }).setProtectedHeader({ alg: "RS256", kid: "fixture-key" }).setIssuer(issuer).setAudience("fixture-client").setSubject("fixture-alice").setIssuedAt().setExpirationTime("5m").sign(privateKey);
        const accessToken = await new SignJWT({}).setProtectedHeader({ alg: "RS256", kid: "fixture-key" }).setIssuer(issuer).setAudience("nexora-api").setSubject("fixture-alice").setIssuedAt().setExpirationTime("5m").sign(privateKey);
        return send({ access_token: accessToken, id_token: idToken, token_type: "Bearer", expires_in: 300 });
      }
      await jwtVerify((request.headers.authorization ?? "").replace(/^Bearer /, ""), publicKey, { issuer, audience: "nexora-api" });
      if (url.pathname === "/api/v1/me") return send({ issuer, subject: "fixture-alice" });
      if (url.pathname === "/api/v1/workspaces") {
        if (request.method === "POST") { const entry = { id: randomUUID(), name: JSON.parse(body).name, role: "owner" }; workspaces.set(entry.id, entry); return send(entry, 201); }
        return send({ items: [...workspaces.values()], next_cursor: null });
      }
      const spend = url.pathname.match(/^\/api\/v1\/workspaces\/([^/]+)\/spend(\/budget|\/records)?$/);
      if (spend) {
        const [, workspaceId, resource] = spend;
        const scope = workspaces.get(workspaceId);
        if (!scope) return send({}, 404);
        if (spendUnavailable) return send({}, 503);
        const rows = [...spendRecords.values()]
          .filter(row => row.workspace_id === workspaceId)
          .sort((a, b) => b.occurred_at.localeCompare(a.occurred_at) || b.id.localeCompare(a.id));
        if (resource === "/budget") {
          // Reading spend is a membership right; changing a cap needs spend:manage.
          if (request.method !== "PUT") return send({}, 405);
          if (scope.role === "member") return send({}, 403);
          const input = JSON.parse(body);
          budgets.set(workspaceId, {
            monthly_limit_micros: input.monthly_limit_micros,
            enforcement: input.enforcement,
            alert_thresholds: input.alert_thresholds ?? [],
          });
          raiseAlerts(workspaceId);
          return send({
            workspace_id: workspaceId, ...budgets.get(workspaceId),
            updated_at: "2026-09-20T15:00:00Z",
          });
        }
        if (resource === "/records") {
          const category = url.searchParams.get("category");
          const cursor = url.searchParams.get("cursor");
          const limit = Number(url.searchParams.get("limit") ?? "25");
          let filtered = category ? rows.filter(row => row.category === category) : rows;
          if (cursor) {
            const index = filtered.findIndex(row => row.id === cursor);
            if (index < 0) return send({}, 404);
            filtered = filtered.slice(index + 1);
          }
          return send({
            items: filtered.slice(0, limit),
            next_cursor: filtered.length > limit ? filtered[limit - 1].id : null,
          });
        }
        const budget = budgets.get(workspaceId) ?? null;
        const consumed = rows.reduce((total, row) => total + row.cost_micros, 0);
        const categories = [...new Set(rows.map(row => row.category))].sort().map(name => {
          const group = rows.filter(row => row.category === name);
          return {
            category: name,
            call_count: group.length,
            input_tokens: group.reduce((total, row) => total + row.input_tokens, 0),
            output_tokens: group.reduce((total, row) => total + row.output_tokens, 0),
            cost_micros: group.reduce((total, row) => total + row.cost_micros, 0),
          };
        });
        raiseAlerts(workspaceId);
        return send({
          workspace_id: workspaceId,
          period_start: "2026-09-01T00:00:00Z",
          period_end: "2026-10-01T00:00:00Z",
          consumed_micros: consumed,
          monthly_limit_micros: budget?.monthly_limit_micros ?? null,
          enforcement: budget?.enforcement ?? null,
          remaining_micros: budget ? Math.max(0, budget.monthly_limit_micros - consumed) : null,
          exhausted: budget !== null
            && budget.enforcement === "enforce"
            && consumed >= budget.monthly_limit_micros,
          alert_thresholds: budget?.alert_thresholds ?? [],
          alerts: [...spendAlerts.entries()]
            .filter(([key]) => key.startsWith(workspaceId + ":"))
            .map(([, alert]) => alert)
            .sort((a, b) => a.threshold_percent - b.threshold_percent),
          categories,
        });
      }
      const latestJudge = url.pathname.match(
        /^\/api\/v1\/workspaces\/([^/]+)\/eval-runs\/([^/]+)\/judge-runs\/latest$/,
      );
      if (latestJudge && request.method === "GET") {
        const [, workspaceId, evalRunId] = latestJudge;
        const scope = workspaces.get(workspaceId);
        if (!scope || scope.role === "member") return send({}, scope ? 403 : 404);
        const run = evalRuns.get(evalRunId);
        if (!run || run.workspace_id !== workspaceId) return send({}, 404);
        const judge = [...evalJudgeRuns.values()].reverse().find(
          item => item.workspace_id === workspaceId && item.eval_run_id === evalRunId,
        );
        return judge ? send(judge) : send({}, 404);
      }
      const queueJudge = url.pathname.match(
        /^\/api\/v1\/workspaces\/([^/]+)\/eval-runs\/([^/]+)\/judge-runs$/,
      );
      if (queueJudge && request.method === "POST") {
        const [, workspaceId, evalRunId] = queueJudge;
        const scope = workspaces.get(workspaceId);
        if (!scope || scope.role === "member") return send({}, scope ? 403 : 404);
        const run = evalRuns.get(evalRunId);
        if (!run || run.workspace_id !== workspaceId) return send({}, 404);
        const key = request.headers["idempotency-key"];
        if (typeof key !== "string" || key.length < 8) return send({}, 400);
        const scopedKey = workspaceId + ":" + key;
        const existingId = judgeIdempotency.get(scopedKey);
        if (existingId) return send(evalJudgeRuns.get(existingId), 200);
        const id = randomUUID();
        const judge: EvalJudgeRun = {
          id, workspace_id: workspaceId, eval_run_id: evalRunId, status: "queued",
          case_count: run.case_count, scored_count: 0, judge_provider: null, judge_model: null,
          prompt_version: null, error_code: null, quality_milli: null,
          baseline_quality_milli: null, quality_delta_milli: null, regression_count: 0,
          improvement_count: 0, input_tokens: 0, output_tokens: 0, latency_ms: 0,
          created_at: "2026-09-20T14:00:00Z", finished_at: null, results: [],
        };
        evalJudgeRuns.set(id, judge);
        judgeIdempotency.set(scopedKey, id);
        return send(judge, 202);
      }
      const evaluation = url.pathname.match(/^\/api\/v1\/workspaces\/([^/]+)\/(eval-suites|eval-runs)(?:\/([^/]+))?(\/runs)?$/);
      if (evaluation) {
        const [, workspaceId, kind, resourceId, history] = evaluation;
        const scope = workspaces.get(workspaceId);
        if (!scope) return send({}, 404);
        if ((kind === "eval-runs" || history) && scope.role === "member") return send({}, 403);
        if (history && historyUnavailable) return send({}, 503);
        if (kind === "eval-runs") {
          const run = evalRuns.get(resourceId);
          return run?.workspace_id === workspaceId ? send(run) : send({}, 404);
        }
        const suite = evalSuites.get(resourceId);
        if (resourceId && suite?.workspace_id !== workspaceId) return send({}, 404);
        if (resourceId && !history) return send(suite);
        const limit = Number(url.searchParams.get("limit") ?? "25");
        const cursor = url.searchParams.get("cursor");
        let rows: Array<EvalSuite | EvalRun> = history
          ? [...evalRuns.values()].filter(run => run.workspace_id === workspaceId && run.suite_id === resourceId)
            .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id))
          : [...evalSuites.values()].filter(item => item.workspace_id === workspaceId).sort((a, b) => a.id.localeCompare(b.id));
        if (cursor) {
          const index = rows.findIndex(row => row.id === cursor);
          if (index < 0) return send({}, 404);
          rows = rows.slice(index + 1);
        }
        return send({ items: rows.slice(0, limit), next_cursor: rows.length > limit ? rows[limit - 1].id : null });
      }
      const knowledgeRoute = url.pathname.match(
        /^\/api\/v1\/workspaces\/([^/]+)\/knowledge\/(sources|ingestions)(?:\/(.+))?$/,
      );
      if (knowledgeRoute) {
        const [, workspaceId, kind, resource] = knowledgeRoute;
        const scope = workspaces.get(workspaceId);
        if (!scope) return send({}, 404);
        if (kind === "ingestions") {
          const job = ingestions.get(resource);
          return job?.workspace_id === workspaceId ? send(job) : send({}, 404);
        }
        if (request.method === "POST") {
          // Adding or removing a source is knowledge:manage; reading the list is not.
          if (scope.role === "member") return send({}, 403);
          const key = request.headers["idempotency-key"];
          if (typeof key !== "string" || key.length < 8) return send({}, 400);
          const scopedKey = workspaceId + ":" + key;
          const existingId = ingestionIdempotency.get(scopedKey);
          if (existingId) return send(ingestions.get(existingId), 200);
          const input = JSON.parse(body);
          const job: Ingestion = {
            id: randomUUID(), workspace_id: workspaceId, source_key: input.source_key,
            version: input.version, title: input.title, access_scope: input.access_scope,
            status: "queued", attempt_count: 0, source_id: null, chunk_count: null,
            embedding_input_tokens: null, error_code: null,
            created_at: "2026-09-20T17:00:00Z", updated_at: "2026-09-20T17:00:00Z",
            finished_at: null,
          };
          ingestions.set(job.id, job);
          ingestionIdempotency.set(scopedKey, job.id);
          return send(job, 202);
        }
        if (request.method === "DELETE") {
          if (scope.role === "member") return send({}, 403);
          const key = decodeURIComponent(resource ?? "");
          const existing = [...sources.values()].find(
            row => row.workspace_id === workspaceId && row.source_key === key,
          );
          if (!existing) return send({}, 404);
          sources.delete(existing.id);
          response.writeHead(204);
          response.end();
          return;
        }
        let rows = [...sources.values()]
          .filter(row => row.workspace_id === workspaceId)
          .sort((a, b) => a.id.localeCompare(b.id));
        const cursor = url.searchParams.get("cursor");
        if (cursor) {
          const index = rows.findIndex(row => row.id === cursor);
          if (index < 0) return send({}, 404);
          rows = rows.slice(index + 1);
        }
        const limit = Number(url.searchParams.get("limit") ?? "25");
        return send({
          items: rows.slice(0, limit),
          next_cursor: rows.length > limit ? rows[limit - 1].id : null,
        });
      }

      const toolRoute = url.pathname.match(
        /^\/api\/v1\/workspaces\/([^/]+)\/tools(?:\/([^/]+))?(\/policy)?$/,
      );
      if (toolRoute) {
        const [, workspaceId, toolName, policy] = toolRoute;
        const scope = workspaces.get(workspaceId);
        if (!scope) return send({}, 404);
        if (!toolName) {
          const rows = [...tools.values()]
            .filter(tool => tool.workspace_id === workspaceId)
            .sort((a, b) => a.name.localeCompare(b.name));
          const limit = Number(url.searchParams.get("limit") ?? "25");
          return send({
            items: rows.slice(0, limit),
            next_cursor: rows.length > limit ? rows[limit - 1].name : null,
          });
        }
        const tool = [...tools.values()].find(
          item => item.workspace_id === workspaceId && item.name === toolName,
        );
        if (!tool) return send({}, 404);
        if (policy) {
          // Reading a policy is a membership right; setting one is tool:manage.
          if (request.method !== "PUT") return send({}, 405);
          if (scope.role === "member") return send({}, 403);
          const input = JSON.parse(body);
          tool.policy_decision = input.decision;
          tool.policy_reason = input.reason;
          tool.policy_updated_at = "2026-09-20T16:00:00Z";
          return send({
            workspace_id: workspaceId, tool_id: tool.id, decision: input.decision,
            reason: input.reason, updated_at: tool.policy_updated_at,
          });
        }
        return send(tool);
      }

      const approvalRoute = url.pathname.match(
        /^\/api\/v1\/workspaces\/([^/]+)\/approvals(?:\/([^/]+)\/decision)?$/,
      );
      if (approvalRoute) {
        const [, workspaceId, approvalId] = approvalRoute;
        const scope = workspaces.get(workspaceId);
        if (!scope) return send({}, 404);
        // Deciding and listing both require tool:approve, which a member never has.
        if (scope.role === "member") return send({}, 403);
        if (approvalId) {
          if (request.method !== "POST") return send({}, 405);
          const approval = approvals.get(approvalId);
          if (!approval || approval.workspace_id !== workspaceId) return send({}, 404);
          if (approval.status !== "pending") return send({}, 409);
          approval.status = JSON.parse(body).decision;
          approval.decided_at = "2026-09-20T16:05:00Z";
          approval.approver_subject = "fixture-alice";
          return send(approval);
        }
        const status = url.searchParams.get("status");
        let rows = [...approvals.values()]
          .filter(row => row.workspace_id === workspaceId)
          .filter(row => !status || row.status === status)
          .sort((a, b) => a.id.localeCompare(b.id));
        const cursor = url.searchParams.get("cursor");
        if (cursor) {
          const index = rows.findIndex(row => row.id === cursor);
          if (index < 0) return send({}, 404);
          rows = rows.slice(index + 1);
        }
        const limit = Number(url.searchParams.get("limit") ?? "25");
        return send({
          items: rows.slice(0, limit),
          next_cursor: rows.length > limit ? rows[limit - 1].id : null,
        });
      }

      const agentRoute = url.pathname.match(/^\/api\/v1\/workspaces\/([^/]+)\/agents$/);
      if (agentRoute) {
        const [, workspaceId] = agentRoute;
        const scope = workspaces.get(workspaceId);
        if (!scope) return send({}, 404);
        if (request.method === "POST") {
          // Defining an agent is agent:manage; starting a run is not.
          if (scope.role === "member") return send({}, 403);
          const input = JSON.parse(body);
          const entry: Agent = {
            id: randomUUID(), workspace_id: workspaceId, name: input.name,
            instructions: input.instructions, model_profile: input.model_profile ?? "default",
            created_at: "2026-09-20T09:00:00Z",
          };
          agents.set(entry.id, entry);
          return send(entry, 201);
        }
        const rows = [...agents.values()].filter(item => item.workspace_id === workspaceId);
        const limit = Number(url.searchParams.get("limit") ?? "25");
        return send({
          items: rows.slice(0, limit),
          next_cursor: rows.length > limit ? rows[limit - 1].id : null,
        });
      }

      const runRoute = url.pathname.match(
        /^\/api\/v1\/workspaces\/([^/]+)\/runs(?:\/([^/]+))?(\/events|\/result|\/cancel)?$/,
      );
      if (runRoute) {
        const [, workspaceId, runId, resource] = runRoute;
        const scope = workspaces.get(workspaceId);
        if (!scope) return send({}, 404);
        if (!runId) {
          if (request.method === "POST") {
            const key = request.headers["idempotency-key"];
            if (typeof key !== "string" || key.length < 8) return send({}, 400);
            const scopedKey = workspaceId + ":" + key;
            const existingId = runIdempotency.get(scopedKey);
            if (existingId) return send(runs.get(existingId), 200);
            const agent = agents.get(JSON.parse(body).agent_id);
            if (!agent || agent.workspace_id !== workspaceId) return send({}, 404);
            const id = randomUUID();
            const now = new Date().toISOString();
            const row = {
              id, workspace_id: workspaceId, agent_id: agent.id, agent_name: agent.name,
              trace_id: randomUUID(), status: "queued", attempt_count: 0, requested_by_me: true,
              cancel_requested_at: null, finished_at: null, failure_code: null,
              created_at: now, updated_at: now,
            };
            runs.set(id, row);
            runEvents.set(id, [{
              id: randomUUID(), event_no: 1, event_type: "run.queued",
              payload: { request_id: "fixture-request" }, created_at: now,
            }]);
            runIdempotency.set(scopedKey, id);
            return send(row, 201);
          }
          let rows = [...runs.values()].filter(row => row.workspace_id === workspaceId);
          const status = url.searchParams.get("status");
          if (status) rows = rows.filter(row => row.status === status);
          const agentFilter = url.searchParams.get("agent_id");
          if (agentFilter) rows = rows.filter(row => row.agent_id === agentFilter);
          if (url.searchParams.get("requested_by_me") === "true") {
            rows = rows.filter(row => row.requested_by_me);
          }
          rows.sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
          const cursor = url.searchParams.get("cursor");
          if (cursor) {
            const index = rows.findIndex(row => row.id === cursor);
            if (index < 0) return send({}, 404);
            rows = rows.slice(index + 1);
          }
          const limit = Number(url.searchParams.get("limit") ?? "25");
          return send({
            items: rows.slice(0, limit),
            next_cursor: rows.length > limit ? rows[limit - 1].id : null,
          });
        }
        const run = runs.get(runId);
        if (!run || run.workspace_id !== workspaceId) return send({}, 404);
        if (resource === "/events") {
          return send({ items: runEvents.get(runId) ?? [], next_cursor: null });
        }
        if (resource === "/result") {
          // Workspace admin never overrides the original requester on raw output.
          if (!run.requested_by_me) return send({}, 404);
          if (!["succeeded", "failed", "cancelled"].includes(run.status)) return send({}, 409);
          return send(runResults.get(runId) ?? {
            run_id: runId, workspace_id: workspaceId, agent_id: run.agent_id,
            trace_id: run.trace_id, status: run.status, output_text: null, finish_reason: null,
            failure_code: run.failure_code, recorded_input_tokens: 0, recorded_output_tokens: 0,
            selected_tools: [], model_steps: [],
          });
        }
        if (resource === "/cancel") {
          if (request.method !== "POST") return send({}, 405);
          if (!["succeeded", "failed", "cancelled"].includes(run.status)) {
            const now = new Date().toISOString();
            run.status = "cancelled";
            run.cancel_requested_at = now;
            run.finished_at = now;
            run.updated_at = now;
            const events = runEvents.get(runId) ?? [];
            events.push({
              id: randomUUID(), event_no: events.length + 1, event_type: "run.cancelled",
              payload: { reason: "requested" }, created_at: now,
            });
            runEvents.set(runId, events);
          }
          return send(run);
        }
        return send(run);
      }

      const match = url.pathname.match(/^\/api\/v1\/workspaces\/([^/]+)(\/members)?$/);
      const workspace = match && workspaces.get(match[1]);
      if (!workspace) return send({}, 404);
      if (match![2]) return workspace.role === "owner" ? send(JSON.parse(body)) : send({}, 403);
      if (request.method === "PATCH") { if (workspace.role === "member") return send({}, 403); workspace.name = JSON.parse(body).name; }
      return send(workspace);
    } catch { send({ error: "unauthorized" }, 401); }
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  issuer = `http://127.0.0.1:${(server.address() as AddressInfo).port}/`;
  return { issuer, workspaces, agents, runs, runEvents, runResults, tools, approvals,
    sources, ingestions,
    evalSuites, evalRuns, evalJudgeRuns, spendRecords, budgets,
    spendAlerts,
    historyUnavailable: (value: boolean) => { historyUnavailable = value; },
    spendUnavailable: (value: boolean) => { spendUnavailable = value; },
    wrongNonce: (value: boolean) => { wrongNonce = value; }, close: () => new Promise<void>(resolve => server.close(() => resolve())) };
}
