import { test } from "node:test";
import assert from "node:assert/strict";
import { fetchHealth } from "../lib/health.ts";
const payload = { status: "ok", service: "nexora-api", version: "0.1.0", dependencies: { postgres: "up", redis: "up" } };
const respond = (body: unknown, status = 200) => (async () => new Response(JSON.stringify(body), { status })) as typeof fetch;
test("validates live API response", async () => {
  assert.equal((await fetchHealth("http://localhost", respond(payload))).kind, "connected");
});
test("retains partial outage details", async () => {
  const state = await fetchHealth("http://localhost", respond({ ...payload, status: "degraded", dependencies: { postgres: "down", redis: "up" } }, 503));
  assert.equal(state.kind, "connected");
  if (state.kind === "connected") assert.equal(state.health.dependencies.postgres, "down");
});
test("rejects malformed and contradictory health payloads", async () => {
  for (const body of [{ status: "ok" }, { ...payload, status: "degraded" }]) {
    assert.equal((await fetchHealth("http://localhost", respond(body))).kind, "invalid");
  }
});
test("represents authorization and connectivity failures", async () => {
  assert.equal((await fetchHealth("http://localhost", respond({}, 403))).kind, "forbidden");
  assert.equal((await fetchHealth("http://localhost", respond({}, 500))).kind, "unavailable");
  const offline = (async () => { throw new Error("offline"); }) as typeof fetch;
  assert.equal((await fetchHealth("http://localhost", offline)).kind, "unavailable");
});
