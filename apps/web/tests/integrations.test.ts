import test from "node:test";
import assert from "node:assert/strict";
import {
  connectInput,
  connectionTestSchema,
  definitionSchema,
  integrationPageSchema,
  integrationSchema,
  integrationStatusKey,
  needsAttention,
  orderedFields,
  scopeReport,
} from "../lib/integration-contracts.ts";
import { createUi } from "../lib/i18n/messages.ts";

const en = createUi("en");
const tr = createUi("tr");
const id = "6f1c9a2e-1b3d-4f5a-8c7e-9d0b1a2c3d4e";
const workspace = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";

const definition = {
  id: "mikro", name: "Mikro ERP", description: "Stock and invoice records.",
  category: "erp", icon: "database", auth_type: "api_key", status: "available",
  version: "1.0.0", capabilities: ["stock.read"], scopes: [],
  credential_fields: [
    { key: "api_key", label: "Service API key", secret: true, required: true },
    { key: "base_url", label: "Service URL", secret: false, required: true },
  ],
};

const integration = {
  id, workspace_id: workspace, integration_definition_id: "mikro",
  display_name: "Customer A", account_identifier: "customer-a", auth_type: "api_key",
  status: "connected", scopes: [], granted_scopes: [], config: { base_url: "https://erp.test" },
  credential_hint: "••••••••A93X",
  credential_expires_at: null,
  created_at: "2026-09-20T09:00:00Z", updated_at: "2026-09-20T09:00:00Z",
  last_tested_at: null, last_success_at: null, last_error: null,
};

test("a connection is accepted only when its credential is a masked hint", () => {
  assert.equal(integrationSchema.parse(integration).credential_hint?.endsWith("A93X"), true);
  // A payload carrying what looks like a real secret is a contract violation, not data
  // the console renders.
  assert.equal(
    integrationSchema.safeParse({ ...integration, credential_hint: "live-key-A93X" }).success,
    false,
  );
  assert.equal(
    integrationSchema.safeParse({ ...integration, credential_hint: null }).success,
    true,
  );
});

test("a connection page is validated end to end", () => {
  const page = integrationPageSchema.parse({ items: [integration], next_cursor: null });
  assert.equal(page.items.length, 1);
  assert.equal(
    integrationPageSchema.safeParse({ items: [{ ...integration, status: "unknown" }], next_cursor: null }).success,
    false,
  );
});

test("a connector definition describes fields but never values", () => {
  const parsed = definitionSchema.parse(definition);
  // Identifying fields come first so the form reads the way it is filled in.
  assert.deepEqual(orderedFields(parsed).map(field => field.key), ["base_url", "api_key"]);
  assert.equal(orderedFields(parsed).at(-1)?.secret, true);
});

test("connection input carries only declared, bounded values", () => {
  assert.equal(connectInput.safeParse({
    integration_definition_id: "mikro", display_name: "Customer A",
    account_identifier: "customer-a", credentials: { api_key: "k", base_url: "https://erp.test" },
  }).success, true);
  assert.equal(connectInput.safeParse({
    integration_definition_id: "Mikro", display_name: "x", account_identifier: "y",
    credentials: {},
  }).success, false);
  assert.equal(connectInput.safeParse({
    integration_definition_id: "mikro", display_name: "x", account_identifier: "y",
    credentials: { api_key: "k".repeat(8193) },
  }).success, false);
});

test("every connection status is written out in both languages", () => {
  for (const status of ["pending", "connected", "expired", "revoked", "error", "disabled"] as const) {
    const key = integrationStatusKey(status);
    assert.notEqual(en.t(key), key);
    assert.notEqual(tr.t(key), key);
    assert.notEqual(en.t(key), tr.t(key));
  }
});

test("a connection that stopped working is surfaced for the tenant to act on", () => {
  assert.equal(needsAttention(integrationSchema.parse(integration)), false);
  for (const status of ["expired", "error", "revoked"] as const) {
    assert.equal(needsAttention(integrationSchema.parse({ ...integration, status })), true);
  }
  assert.equal(
    needsAttention(integrationSchema.parse({ ...integration, status: "disabled" })),
    false,
  );
});

test("missing permissions are named rather than implied", () => {
  const asked = integrationSchema.parse({
    ...integration,
    scopes: ["instagram_basic", "instagram_content_publish"],
    granted_scopes: ["instagram_basic"],
  });
  assert.deepEqual(scopeReport(asked), {
    granted: ["instagram_basic"], missing: ["instagram_content_publish"],
  });
  // Before a provider answers, what was asked for is the best available account.
  const unanswered = integrationSchema.parse({ ...integration, scopes: ["a"], granted_scopes: [] });
  assert.deepEqual(scopeReport(unanswered), { granted: ["a"], missing: [] });
});

test("a connection test result is validated", () => {
  const result = connectionTestSchema.parse({
    integration_id: id, status: "expired", ok: false,
    checked_at: "2026-09-21T00:31:00Z", granted_scopes: [], missing_scopes: [],
    error_code: "unauthorized",
  });
  assert.equal(result.ok, false);
  assert.equal(result.error_code, "unauthorized");
});
