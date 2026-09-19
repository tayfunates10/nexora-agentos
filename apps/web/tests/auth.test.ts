import { test } from "node:test";
import assert from "node:assert/strict";
import { cookieNames, cookieOptions, endSession, loadSession, opaqueId, readConfig, sessionKey, validMutation, validRequestSource } from "../lib/auth-core.ts";
import { memberInput, workspaceInput } from "../lib/workspace-contracts.ts";
import type { AuthConfig, Store } from "../lib/auth-core.ts";
const config: AuthConfig = { origin: "https://nexora.test", issuer: "https://id.test/", clientId: "client", clientSecret: "secret", audience: "api", apiUrl: "http://localhost:8000", redisUrl: "redis://localhost" };
export class MemoryStore implements Store {
  values = new Map<string, string>();
  async get(key: string) { return this.values.get(key) ?? null; }
  async take(key: string) { const value = await this.get(key); this.values.delete(key); return value; }
  async put(key: string, value: string, _ttl: number) { this.values.set(key, value); }
  async remove(key: string) { this.values.delete(key); }
}
test("auth configuration fails closed and requires secure remote URLs", () => {
  assert.equal(readConfig({}), null);
  const env = { NEXORA_WEB_ORIGIN: "https://nexora.test", NEXORA_AUTH_ISSUER: "https://id.test/", NEXORA_OIDC_CLIENT_ID: "client", NEXORA_OIDC_CLIENT_SECRET: "secret", NEXORA_AUTH_AUDIENCE: "api", NEXORA_SESSION_REDIS_URL: "redis://localhost" };
  assert.equal(readConfig(env)?.origin, config.origin);
  for (const origin of ["http://nexora.test", "https://nexora.test/path", "https://user:pass@nexora.test"]) assert.throws(() => readConfig({ ...env, NEXORA_WEB_ORIGIN: origin }));
});
test("request source accepts exact Origin or a same-origin Referer fallback", () => {
  assert.equal(validRequestSource(config.origin, null, config), true);
  assert.equal(validRequestSource(null, config.origin + "/login", config), true);
  assert.equal(validRequestSource(null, config.origin + ".evil.test/login", config), false);
  assert.equal(validRequestSource("https://evil.test", config.origin + "/login", config), false);
  assert.equal(validRequestSource(null, null, config), false);
  assert.equal(validRequestSource(null, "not a url", config), false);
});
test("mutations require a trusted request source and constant-time CSRF match", () => {
  assert.equal(validMutation(config.origin, config, "abc", "abc"), true);
  assert.equal(validMutation(null, config, "abc", "abc", config.origin + "/workspaces"), true);
  for (const [origin, token] of [[null, "abc"], ["https://evil.test", "abc"], [config.origin, "wrong"], [config.origin, "éab"]]) assert.equal(validMutation(origin, config, token!, "abc"), false);
  assert.equal(validMutation("https://evil.test", config, "abc", "abc", config.origin + "/workspaces"), false);
});
test("production cookies are host-scoped, secure, HTTP-only and SameSite Lax", () => {
  assert.equal(cookieNames(config).session, "__Host-nexora_session");
  assert.deepEqual(cookieOptions(config, 60), { httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: 60 });
});
test("sessions expire, reject malformed IDs, and disappear on logout", async () => {
  const store = new MemoryStore(); const id = opaqueId();
  const session = { accessToken: "server-secret", subject: "alice", issuer: config.issuer, csrf: opaqueId(), expiresAt: Date.now() + 60000 };
  await store.put(sessionKey(id), JSON.stringify(session), 60);
  assert.equal((await loadSession(store, id))?.subject, "alice");
  assert.equal(await loadSession(store, "invalid"), null);
  await endSession(store, id); assert.equal(await loadSession(store, id), null);
  await store.put(sessionKey(id), JSON.stringify({ ...session, expiresAt: 1 }), 60);
  assert.equal(await loadSession(store, id), null); assert.equal(store.values.size, 0);
});
test("workspace form validation blocks empty names and owner grants", () => {
  assert.equal(workspaceInput.safeParse({ name: "  " }).success, false);
  assert.equal(workspaceInput.safeParse({ name: "x".repeat(101) }).success, false);
  assert.equal(memberInput.safeParse({ subject: "alice", role: "owner" }).success, false);
  assert.equal(memberInput.safeParse({ subject: "alice", role: "admin" }).success, true);
});
