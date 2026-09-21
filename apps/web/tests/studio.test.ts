import test from "node:test";
import assert from "node:assert/strict";
import {
  catalogAgentInput,
  catalogAgentPageSchema,
  catalogAgentSchema,
  catalogVersionPageSchema,
  nextStep,
  parseManifest,
  rolloutInput,
  rolloutPageSchema,
  rolloutStateKey,
  ROLLOUT_STEPS,
} from "../lib/studio-contracts.ts";
import { createUi } from "../lib/i18n/messages.ts";

const en = createUi("en");
const tr = createUi("tr");
const id = "6f1c9a2e-1b3d-4f5a-8c7e-9d0b1a2c3d4e";
const other = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";

const agent = {
  id, slug: "social-media", name: "Social Media Management",
  description: "Manage connected social accounts.", category: "marketing", icon: "megaphone",
  status: "stable", visibility: "public", latest_version: "1.4.2", version_count: 7,
  created_at: "2026-09-20T09:00:00Z", updated_at: "2026-09-20T09:00:00Z",
};

test("a catalog entry without a published version is valid", () => {
  assert.equal(catalogAgentSchema.parse(agent).latest_version, "1.4.2");
  const fresh = catalogAgentSchema.parse({
    ...agent, latest_version: null, version_count: 0, status: "draft", visibility: "restricted",
  });
  assert.equal(fresh.version_count, 0);
  assert.equal(
    catalogAgentPageSchema.parse({ items: [agent], next_cursor: null }).items.length, 1,
  );
});

test("a catalog entry is created with a slug the platform would accept", () => {
  const body = {
    slug: "accounting", name: "Accounting", description: "Reads the ledger.",
    category: "finance", icon: "ledger", status: "draft", visibility: "restricted",
  };
  assert.equal(catalogAgentInput.safeParse(body).success, true);
  for (const slug of ["Accounting", "ac", "accounting-", "-accounting", "accounting_agent"]) {
    assert.equal(catalogAgentInput.safeParse({ ...body, slug }).success, false, slug);
  }
});

test("a pasted manifest is bounded and must belong to the agent it is published to", () => {
  const manifest = JSON.stringify({ slug: "social-media", version: "1.5.0", name: "x" });
  assert.equal(parseManifest(manifest, "social-media").version, "1.5.0");
  // Publishing one agent's manifest under another would create a version nobody meant.
  assert.throws(() => parseManifest(manifest, "seo"), /different catalog agent/);
  assert.throws(() => parseManifest("not json", "social-media"));
  assert.throws(() => parseManifest("[]", "social-media"), /JSON object/);
  assert.throws(
    () => parseManifest(JSON.stringify({ slug: "social-media" }), "social-media"),
    /state a version/,
  );
  assert.throws(() => parseManifest("x".repeat(200_001), "social-media"), /too large/);
});

test("a rollout is started at a bounded reach on a known channel", () => {
  assert.deepEqual(
    rolloutInput.parse({ version: "1.5.0", channel: "stable", percentage: "25" }),
    { version: "1.5.0", channel: "stable", percentage: 25 },
  );
  assert.equal(rolloutInput.safeParse({
    version: "1.5", channel: "stable", percentage: "25" }).success, false);
  assert.equal(rolloutInput.safeParse({
    version: "1.5.0", channel: "nightly", percentage: "25" }).success, false);
  assert.equal(rolloutInput.safeParse({
    version: "1.5.0", channel: "stable", percentage: "101" }).success, false);
});

test("widening a rollout always moves forward and stops at everyone", () => {
  assert.deepEqual(ROLLOUT_STEPS, [5, 25, 50, 100]);
  assert.equal(nextStep(0), 5);
  assert.equal(nextStep(5), 25);
  assert.equal(nextStep(25), 50);
  assert.equal(nextStep(50), 100);
  // Already at everyone: there is no wider step to offer.
  assert.equal(nextStep(100), null);
  // A reach set by hand still only ever moves up.
  assert.equal(nextStep(30), 50);
});

test("a rollout and a version listing are validated", () => {
  const rollouts = rolloutPageSchema.parse({
    items: [{
      id, catalog_agent_id: other, version_id: other, version: "1.5.0", channel: "stable",
      percentage: 25, state: "active",
      created_at: "2026-09-20T09:00:00Z", updated_at: "2026-09-20T09:00:00Z",
    }],
    next_cursor: null,
  });
  assert.equal(rollouts.items[0].state, "active");
  const versions = catalogVersionPageSchema.parse({
    items: [{
      id, version: "1.5.0", status: "stable", channel: "stable", min_runtime_version: "1.0.0",
      changelog: "Publishing retry fixed.", created_at: "2026-09-20T09:00:00Z",
    }],
    next_cursor: null,
  });
  assert.equal(versions.items[0].min_runtime_version, "1.0.0");
});

test("every rollout state reads in both languages", () => {
  for (const state of ["active", "paused", "completed", "rolled_back"] as const) {
    const key = rolloutStateKey(state);
    assert.notEqual(en.t(key), key);
    assert.notEqual(tr.t(key), key);
  }
});
