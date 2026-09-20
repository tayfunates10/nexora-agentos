// Test-only OIDC issuer and API fixture. Never imported by application routes.
import { createServer } from "node:http";
import { createHash, randomUUID } from "node:crypto";
import { generateKeyPair, exportJWK, SignJWT, jwtVerify } from "jose";
import type { AddressInfo } from "node:net";
import type { EvalJudgeRun, EvalRun, EvalSuite } from "../../lib/evaluation-contracts.ts";

export async function startProvider() {
  const { privateKey, publicKey } = await generateKeyPair("RS256");
  const jwk = { ...await exportJWK(publicKey), kid: "fixture-key", alg: "RS256", use: "sig" };
  const codes = new Map<string, { nonce: string; challenge: string; redirect: string }>();
  const workspaces = new Map<string, { id: string; name: string; role: string }>();
  const evalSuites = new Map<string, EvalSuite>();
  const evalRuns = new Map<string, EvalRun>();
  const evalJudgeRuns = new Map<string, EvalJudgeRun>();
  const judgeIdempotency = new Map<string, string>();
  let historyUnavailable = false;
  let issuer = "";
  let wrongNonce = false;
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
          model_cost_usd_picos: "0", model_cost_call_count: 0,
          model_cost_pricing_complete: true, model_cost_pricing_versions: [],
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
  return { issuer, workspaces, evalSuites, evalRuns, evalJudgeRuns,
    historyUnavailable: (value: boolean) => { historyUnavailable = value; },
    wrongNonce: (value: boolean) => { wrongNonce = value; }, close: () => new Promise<void>(resolve => server.close(() => resolve())) };
}
