// Test-only OIDC issuer and API fixture. Never imported by application routes.
import { createServer } from "node:http";
import { createHash, randomUUID } from "node:crypto";
import { generateKeyPair, exportJWK, SignJWT, jwtVerify } from "jose";
import type { AddressInfo } from "node:net";
import type { EvalJudgeRun, EvalRun, EvalSuite } from "../../lib/evaluation-contracts.ts";
import type { SpendRecord } from "../../lib/spend-contracts.ts";

export async function startProvider() {
  const { privateKey, publicKey } = await generateKeyPair("RS256");
  const jwk = { ...await exportJWK(publicKey), kid: "fixture-key", alg: "RS256", use: "sig" };
  const codes = new Map<string, { nonce: string; challenge: string; redirect: string }>();
  const workspaces = new Map<string, { id: string; name: string; role: string }>();
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
  return { issuer, workspaces, evalSuites, evalRuns, evalJudgeRuns, spendRecords, budgets,
    spendAlerts,
    historyUnavailable: (value: boolean) => { historyUnavailable = value; },
    spendUnavailable: (value: boolean) => { spendUnavailable = value; },
    wrongNonce: (value: boolean) => { wrongNonce = value; }, close: () => new Promise<void>(resolve => server.close(() => resolve())) };
}
