import test from "node:test";
import assert from "node:assert/strict";
import {
  catalogEntrySchema,
  catalogPageSchema,
  compareVersions,
  instanceStatusKey,
  readinessSchema,
  tenantAgentSchema,
  tenantAgentSummarySchema,
  unmetRequirements,
  updateAvailable,
  versionEventPageSchema,
} from "../lib/catalog-contracts.ts";
import { createUi } from "../lib/i18n/messages.ts";

const en = createUi("en");
const tr = createUi("tr");
const id = "6f1c9a2e-1b3d-4f5a-8c7e-9d0b1a2c3d4e";
const other = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";

const entry = {
  catalog_agent_id: id, slug: "social-media", name: "Social Media Management",
  description: "Manage connected social accounts.", category: "marketing", icon: "megaphone",
  status: "stable", available_version: "1.4.2", installed_count: 1,
  required_integrations: ["instagram"], optional_integrations: ["facebook"],
  capabilities: ["social.analytics.read"], approval_required: ["social.post.publish"],
};

const manifest = {
  id: "nexora.social-media", slug: "social-media", name: "Social Media Management",
  description: "Manage connected social accounts.", version: "1.4.2",
  min_runtime_version: "1.0.0", system_instructions: "Work only from tool output.",
  capabilities: ["social.analytics.read"], approval_required: ["social.post.publish"],
  required_integrations: ["instagram"], optional_integrations: ["facebook"],
  changelog: "Publishing retry fixed.", settings_schema: { type: "object" },
};

const requirement = (overrides: Record<string, unknown> = {}) => ({
  integration_definition_id: "instagram", name: "instagram", required: true,
  bound_integration_id: null, bound_display_name: null, bound_status: null, satisfied: false,
  ...overrides,
});

const agent = {
  id, workspace_id: other, catalog_agent_id: other, slug: "social-media",
  display_name: "Arma social", version: "1.4.2", available_version: "1.5.0",
  status: "paused", update_channel: "stable", update_mode: "manual",
  instructions_override: null, settings: {}, manifest, bindings: [],
  readiness: { ready: false, requirements: [requirement()], missing_required: ["instagram"] },
  created_at: "2026-09-20T09:00:00Z", updated_at: "2026-09-20T09:00:00Z",
};

test("versions are compared numerically, not as text", () => {
  assert.equal(compareVersions("1.10.0", "1.9.0"), 1);
  assert.equal(compareVersions("1.9.0", "1.10.0"), -1);
  assert.equal(compareVersions("2.0.0", "2.0.0"), 0);
  assert.equal(compareVersions("1.4.10", "1.4.9"), 1);
});

test("an update is offered only when the catalog is genuinely ahead", () => {
  assert.equal(updateAvailable({ version: "1.4.2", available_version: "1.5.0" }), true);
  assert.equal(updateAvailable({ version: "1.5.0", available_version: "1.5.0" }), false);
  // A tenant pinned back to an older version is not told to "update" to what it left.
  assert.equal(updateAvailable({ version: "1.5.0", available_version: "1.4.2" }), false);
  assert.equal(updateAvailable({ version: "1.4.2", available_version: null }), false);
});

test("a catalog entry carries everything a screen needs to render an unknown agent", () => {
  const parsed = catalogEntrySchema.parse(entry);
  assert.deepEqual(parsed.required_integrations, ["instagram"]);
  assert.deepEqual(parsed.approval_required, ["social.post.publish"]);
  assert.equal(catalogPageSchema.parse({ items: [entry], next_cursor: null }).items.length, 1);
  // An agent nobody has written a screen for still validates: the catalog is data.
  assert.equal(catalogEntrySchema.safeParse({
    ...entry, slug: "accounting", name: "Accounting", category: "finance",
    required_integrations: ["mikro"], capabilities: ["ledger.read"],
  }).success, true);
});

test("readiness must agree with the requirements it lists", () => {
  assert.equal(readinessSchema.safeParse({
    ready: true, requirements: [], missing_required: [],
  }).success, true);
  // A payload claiming readiness while naming something missing is refused outright.
  assert.equal(readinessSchema.safeParse({
    ready: true, requirements: [requirement()], missing_required: ["instagram"],
  }).success, false);
  assert.equal(readinessSchema.safeParse({
    ready: false, requirements: [requirement()], missing_required: [],
  }).success, false);
});

test("only unmet required connections are asked for", () => {
  const parsed = tenantAgentSchema.parse({
    ...agent,
    readiness: {
      ready: false,
      requirements: [
        requirement({ satisfied: true, bound_integration_id: other,
          bound_display_name: "Customer A", bound_status: "connected" }),
        requirement({ integration_definition_id: "facebook", name: "facebook", required: false }),
        requirement({ integration_definition_id: "mikro", name: "mikro" }),
      ],
      missing_required: ["mikro"],
    },
  });
  assert.deepEqual(
    unmetRequirements(parsed).map(item => item.integration_definition_id),
    ["mikro"],
  );
});

test("an installed agent and its summary are validated", () => {
  assert.equal(tenantAgentSchema.parse(agent).manifest.version, "1.4.2");
  assert.equal(tenantAgentSummarySchema.parse({
    id, workspace_id: other, catalog_agent_id: other, slug: "social-media",
    display_name: "Arma social", version: "1.4.2", available_version: "1.5.0",
    status: "active", update_channel: "canary", update_mode: "automatic", ready: true,
    created_at: "2026-09-20T09:00:00Z", updated_at: "2026-09-20T09:00:00Z",
  }).ready, true);
  assert.equal(tenantAgentSchema.safeParse({ ...agent, version: "1.4" }).success, false);
});

test("version history records what ran when", () => {
  const page = versionEventPageSchema.parse({
    items: [
      { id, action: "installed", from_version: null, to_version: "1.0.0",
        actor_subject: "owner", created_at: "2026-09-20T09:00:00Z" },
      { id: other, action: "rolled_back", from_version: "2.0.0", to_version: "1.0.0",
        actor_subject: "owner", created_at: "2026-09-21T09:00:00Z" },
    ],
    next_cursor: null,
  });
  assert.deepEqual(page.items.map(item => item.action), ["installed", "rolled_back"]);
});

test("every instance state and release channel reads in both languages", () => {
  for (const status of ["active", "paused", "disabled"] as const) {
    const key = instanceStatusKey(status);
    assert.notEqual(en.t(key), key);
    assert.notEqual(tr.t(key), key);
  }
  for (const channel of ["stable", "beta", "canary"] as const) {
    assert.notEqual(en.t(`catalog.channel.${channel}`), `catalog.channel.${channel}`);
    assert.notEqual(tr.t(`catalog.channelHelp.${channel}`), `catalog.channelHelp.${channel}`);
  }
});
