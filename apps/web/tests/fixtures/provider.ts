// Test-only OIDC issuer and API fixture. Never imported by application routes.
import { createServer } from "node:http";
import { createHash, randomUUID } from "node:crypto";
import { generateKeyPair, exportJWK, SignJWT, jwtVerify } from "jose";
import type { AddressInfo } from "node:net";

export async function startProvider() {
  const { privateKey, publicKey } = await generateKeyPair("RS256");
  const jwk = { ...await exportJWK(publicKey), kid: "fixture-key", alg: "RS256", use: "sig" };
  const codes = new Map<string, { nonce: string; challenge: string; redirect: string }>();
  const workspaces = new Map<string, { id: string; name: string; role: string }>();
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
  return { issuer, workspaces, wrongNonce: (value: boolean) => { wrongNonce = value; }, close: () => new Promise<void>(resolve => server.close(() => resolve())) };
}
