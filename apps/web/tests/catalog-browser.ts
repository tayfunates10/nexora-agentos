import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type { startProvider } from "./fixtures/provider.ts";
import { t } from "./ui-text.ts";
import { expectNoHorizontalOverflow, settleReveals, sidebarLink } from "./shell.ts";

const SECRET = "mikro-live-service-key-9f3b21ccA93X";
const ROTATED = "mikro-rotated-service-key-77d1ffB71Z";

/**
 * The Integration Center and the agent catalog, exercised as a person uses them.
 *
 * Two promises are checked here rather than assumed: a credential the tenant types never
 * appears anywhere in the rendered console, and an agent stays paused until the account
 * it needs is actually connected and bound.
 */
export async function checkCatalogAndIntegrations(
  page: Page,
  provider: Awaited<ReturnType<typeof startProvider>>,
  detailUrl: string,
) {
  provider.connectors.set("mikro", {
    id: "mikro", name: "Mikro ERP", description: "Stock, customer and invoice records.",
    category: "erp", icon: "database", auth_type: "api_key", status: "available",
    version: "1.0.0", capabilities: ["stock.read", "customers.read"], scopes: [],
    credential_fields: [
      { key: "base_url", label: "Service URL", secret: false, required: true, help: "",
        pattern: null },
      { key: "api_key", label: "Service API key", secret: true, required: true, help: "",
        pattern: null },
    ],
  });
  const manifest = {
    id: "nexora.operations", slug: "operations", name: "Operations",
    description: "Watches operational signals across connected systems.", version: "1.0.0",
    min_runtime_version: "1.0.0", system_instructions: "Summarise what actually changed.",
    capabilities: ["operations.signals.read"],
    approval_required: ["operations.action.execute"],
    required_integrations: ["mikro"], optional_integrations: [],
    changelog: "First release.", settings_schema: { type: "object" },
  };
  provider.catalogAgents.set("operations", {
    catalog_agent_id: randomUUID(), slug: "operations", name: "Operations",
    description: manifest.description, category: "operations", icon: "activity",
    status: "stable", available_version: "1.0.0", installed_count: 0,
    required_integrations: ["mikro"], optional_integrations: [],
    capabilities: manifest.capabilities, approval_required: manifest.approval_required,
    manifest,
  });

  // ------------------------------------------------- connecting an account
  // An earlier check may have left a narrow viewport, where the persistent sidebar is
  // replaced by the drawer. This check starts from the wide layout it asserts against.
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(detailUrl);
  await sidebarLink(page, t("navigation.integrations")).click();
  await expect(page.getByRole("heading", { name: t("integrations.title") })).toBeVisible();
  await expect(page.getByRole("heading", { name: t("integrations.emptyTitle") })).toBeVisible();

  await settleReveals(page);
  const connector = page.locator("article.card").filter({ hasText: "Mikro ERP" });
  await connector.locator("summary").filter({ hasText: t("integrations.connectTitle") }).click();
  await page.getByLabel(t("integrations.displayNameLabel")).fill("Customer A");
  await page.getByLabel(t("integrations.accountLabel")).fill("customer-a");
  await page.getByLabel("Service URL").fill("https://erp.example.com");
  await page.getByLabel("Service API key").fill(SECRET);
  await page.getByRole("button", { name: t("integrations.connectSubmit"), exact: true }).click();

  await expect(page.getByRole("status")).toContainText(t("integrations.connected"));
  await expect(page.getByText(t("integrations.status.connected")).first()).toBeVisible();
  // The whole rendered page, not just the field: the value the person typed is gone.
  expect(await page.content()).not.toContain(SECRET);
  await expect(page.getByText("••••••••A93X")).toBeVisible();
  const stored = [...provider.integrations.values()][0];
  expect(stored.credential_hint).not.toContain(SECRET);
  expect(JSON.stringify(stored)).not.toContain(SECRET);

  // A secret input is never pre-filled, because the server has nothing to fill it with.
  const rotate = page.locator("summary").filter({ hasText: t("integrations.rotateTitle") });
  await rotate.click();
  const rotateForm = page.locator("details.disclosure").filter({ has: rotate });
  const rotateKey = rotateForm.getByLabel("Service API key");
  await expect(rotateKey).toHaveValue("");
  await expect(rotateKey).toHaveAttribute("type", "password");
  await rotateForm.getByLabel("Service URL").fill("https://erp.example.com");
  await rotateKey.fill(ROTATED);
  await page.getByRole("button", { name: t("integrations.rotateSubmit") }).click();
  await expect(page.getByRole("status")).toContainText(t("integrations.rotated"));
  await expect(page.getByText("••••••••B71Z")).toBeVisible();
  expect(await page.content()).not.toContain(ROTATED);

  await page.getByRole("button", { name: t("integrations.test") }).click();
  await expect(page.getByRole("status")).toContainText(t("integrations.tested"));

  // ---------------------------------------------- adding a standard agent
  await sidebarLink(page, t("navigation.catalog")).click();
  await expect(page.getByRole("heading", { name: t("catalog.title") })).toBeVisible();
  await settleReveals(page);
  await expect(page.getByText(t("catalog.installedEmpty"))).toBeVisible();
  await page.getByRole("button", { name: t("catalog.add"), exact: true }).click();

  // Installed, but deliberately not running: its connection has not been chosen yet.
  await expect(page.getByRole("status")).toContainText(t("catalog.added"));
  await expect(page.getByText(t("catalog.instanceStatus.paused"))).toBeVisible();
  await expect(page.getByText(t("catalog.notReady", { count: "1" }))).toBeVisible();

  const agent = [...provider.tenantAgents.values()][0];
  expect(agent.readiness.ready).toBe(false);
  await page.getByRole("button", { name: t("catalog.bindSubmit") }).click();
  await expect(page.getByText(t("catalog.ready"))).toBeVisible();
  await expect(page.getByText(t("catalog.instanceStatus.active"))).toBeVisible();
  expect([...provider.tenantAgents.values()][0].bindings[0].binding_key).toBe("mikro");

  // ------------------------------------------------ updating and rolling back
  await settleReveals(page);
  const instance = [...provider.tenantAgents.values()][0];
  instance.available_version = "1.1.0";
  await page.reload();
  await settleReveals(page);
  await page.getByRole("button", { name: new RegExp(t("catalog.update")) }).click();
  await expect(page.getByRole("status")).toContainText(t("catalog.saved"));
  expect([...provider.tenantAgents.values()][0].version).toBe("1.1.0");

  await settleReveals(page);
  await page.getByRole("button", { name: t("catalog.rollback"), exact: true }).click();
  // Rolling back restores the version, and the tenant's own binding survives it.
  expect([...provider.tenantAgents.values()][0].version).toBe("1.0.0");
  expect([...provider.tenantAgents.values()][0].bindings).toHaveLength(1);
  await expect(page.getByText(t("catalog.historyAction.rolled_back"))).toBeVisible();

  // --------------------------------------------------- settings and forking
  await settleReveals(page);
  await page.getByLabel(t("catalog.instructionsLabel")).fill("Always write in Turkish.");
  await page.getByLabel(t("catalog.settingsLabel")).fill('{"escalation_contact":"ops@example.com"}');
  await page.getByRole("button", { name: t("common.save"), exact: true }).click();
  await expect(page.getByRole("status")).toContainText(t("catalog.saved"));
  expect([...provider.tenantAgents.values()][0].settings)
    .toStrictEqual({ escalation_contact: "ops@example.com" });

  await settleReveals(page);
  await page.locator("summary").filter({ hasText: t("catalog.forkSubmit") }).click();
  await page.getByLabel(t("catalog.forkNameLabel")).fill("Arma Operations Agent");
  await page.getByRole("button", { name: t("catalog.forkSubmit") }).click();
  await expect(page.getByRole("status")).toContainText(t("catalog.forked"));
  const fork = [...provider.agents.values()].find(item => item.name === "Arma Operations Agent");
  expect(fork?.instructions).toBe(manifest.system_instructions);

  // ------------------------------------------- a connection that stops working
  await page.goto(detailUrl + "/integrations");
  await page.getByRole("button", { name: t("integrations.disable"), exact: true }).click();
  await expect(page.getByText(t("integrations.status.disabled"))).toBeVisible();
  // An agent is never left claiming it can run against a switched-off connection.
  await page.goto(`${detailUrl}/catalog/${instance.id}`);
  await expect(page.getByText(t("catalog.notReady", { count: "1" }))).toBeVisible();
  await expect(page.getByText(t("catalog.instanceStatus.paused"))).toBeVisible();

  await page.goto(detailUrl + "/integrations");
  await page.getByRole("button", { name: t("integrations.enable"), exact: true }).click();
  await expect(page.getByText(t("integrations.status.connected")).first()).toBeVisible();

  // --------------------------------------------------- narrow screen layout
  await page.setViewportSize({ width: 320, height: 720 });
  for (const path of ["/integrations", "/catalog", `/catalog/${instance.id}`]) {
    await page.goto(detailUrl + path);
    await settleReveals(page);
    await expectNoHorizontalOverflow(page);
  }
  await page.setViewportSize({ width: 1440, height: 900 });
}

/** A member may look at connections and agents but may change neither. */
export async function checkCatalogReadOnly(page: Page, detailUrl: string) {
  await page.goto(detailUrl + "/integrations");
  await expect(page.getByText(t("integrations.readOnlyNotice"))).toBeVisible();
  await expect(page.getByRole("button", { name: t("integrations.connectSubmit"), exact: true }))
    .toHaveCount(0);
  // Checking whether a connection still works is reading, so it stays available.
  await expect(page.getByRole("button", { name: t("integrations.test") })).toHaveCount(1);
  await expect(page.getByRole("button", { name: t("integrations.disable"), exact: true })).toHaveCount(0);
  await expect(page.locator("summary").filter({ hasText: t("integrations.rotateTitle") }))
    .toHaveCount(0);
  // Reading a connection's state is part of using the agents bound to it.
  await expect(page.getByText(t("integrations.status.connected")).first()).toBeVisible();

  await page.goto(detailUrl + "/catalog");
  await expect(page.getByText(t("catalog.readOnlyNotice")).first()).toBeVisible();
  await expect(page.getByRole("button", { name: t("catalog.add"), exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: t("catalog.policySubmit"), exact: true })).toHaveCount(0);
}
