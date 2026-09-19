import { test } from "node:test";
import assert from "node:assert/strict";
import { beginLogin, finishLogin, loadSession } from "../lib/auth-core.ts";
import { startProvider } from "./fixtures/provider.ts";
import type { Store } from "../lib/auth-core.ts";

test("OIDC code flow validates PKCE, state, nonce, signatures and rejects replay", async () => {
  const provider = await startProvider();
  const values = new Map<string, string>();
  const store: Store = { get: async k => values.get(k) ?? null, take: async k => { const v = values.get(k); values.delete(k); return v ?? null; }, put: async (k,v) => { values.set(k,v); }, remove: async k => { values.delete(k); } };
  const config = { origin: "http://localhost:3000", issuer: provider.issuer, clientId: "fixture-client", clientSecret: "fixture-secret", audience: "nexora-api", apiUrl: provider.issuer, redisUrl: "unused" };
  try {
    const start = await beginLogin(config, store);
    assert.equal(start.url.searchParams.get("code_challenge_method"), "S256");
    const authorization = await fetch(start.url, { redirect: "manual" });
    const callback = new URL(authorization.headers.get("location")!);
    const result = await finishLogin(config, store, start.id, callback);
    assert.equal((await loadSession(store, result.id))?.subject, "fixture-alice");
    assert.ok(result.ttl <= 300);
    await assert.rejects(finishLogin(config, store, start.id, callback));
    const badState = await beginLogin(config, store);
    const response = await fetch(badState.url, { redirect: "manual" });
    const tampered = new URL(response.headers.get("location")!); tampered.searchParams.set("state", "attacker");
    await assert.rejects(finishLogin(config, store, badState.id, tampered));
    provider.wrongNonce(true);
    const badNonce = await beginLogin(config, store);
    const nonceResponse = await fetch(badNonce.url, { redirect: "manual" });
    await assert.rejects(finishLogin(config, store, badNonce.id, new URL(nonceResponse.headers.get("location")!)));
  } finally { await provider.close(); }
});
