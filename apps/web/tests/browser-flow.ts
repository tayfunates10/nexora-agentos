import { chromium, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { startProvider } from "./fixtures/provider.ts";
import { checkAgentRuns } from "./agent-browser.ts";
import { checkCatalogAndIntegrations, checkCatalogReadOnly } from "./catalog-browser.ts";
import { checkEvaluations } from "./evaluation-browser.ts";
import { checkGovernance } from "./governance-browser.ts";
import { checkKnowledge } from "./knowledge-browser.ts";
import { checkSpend } from "./spend-browser.ts";
import { checkLanguageNegotiation, checkPublicShell, checkShell } from "./shell-browser.ts";
import { t } from "./ui-text.ts";
import { expectNoHorizontalOverflow, settleReveals } from "./shell.ts";

const provider = await startProvider();
const origin = "http://127.0.0.1:3100";
const child = spawn(process.execPath, ["../../node_modules/next/dist/bin/next", "start", "--hostname", "127.0.0.1", "--port", "3100"], {
  cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"],
  env: { ...process.env, NEXORA_WEB_ORIGIN: origin, NEXORA_AUTH_ISSUER: provider.issuer,
    NEXORA_OIDC_CLIENT_ID: "fixture-client", NEXORA_OIDC_CLIENT_SECRET: "fixture-secret",
    NEXORA_AUTH_AUDIENCE: "nexora-api", NEXORA_API_URL: provider.issuer,
    NEXORA_SESSION_REDIS_URL: process.env.NEXORA_SESSION_REDIS_URL ?? "redis://127.0.0.1:6379/15" },
});
let logs = ""; child.stdout.on("data", chunk => { logs += chunk; }); child.stderr.on("data", chunk => { logs += chunk; });
let browser;
try {
  let started = false;
  for (let i = 0; i < 80; i++) {
    try { if ((await fetch(origin + "/login")).ok) { started = true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  if (!started) throw new Error("Web startup failed: " + logs);
  // CI installs the browser Playwright pins. An environment that already carries a
  // compatible Chromium can point at it instead of downloading a second copy.
  const executablePath = process.env.NEXORA_CHROMIUM_PATH;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  // A Turkish browser with no stored choice: the default path the product promises.
  const context = await browser.newContext({ locale: "tr-TR" });
  const page = await context.newPage();
  await page.goto(origin + "/workspaces");
  await expect(page).toHaveURL(origin + "/login");
  await page.getByRole("button", { name: t("auth.continue") }).click();
  await expect(page.getByRole("heading", { name: t("workspaces.title") })).toBeVisible();
  await expect(page.getByRole("heading", { name: t("workspaces.emptyTitle") })).toBeVisible();
  const cookies = await context.cookies();
  const session = cookies.find(cookie => cookie.name === "nexora_session");
  expect(session?.httpOnly).toBe(true); expect(session?.sameSite).toBe("Lax");
  expect(session?.value).toMatch(/^[A-Za-z0-9_-]{43}$/);
  await page.getByLabel(t("workspaces.nameLabel")).fill("Browser workspace");
  await page.getByRole("button", { name: t("workspaces.createSubmit") }).click();
  await expect(page.getByRole("link", { name: "Browser workspace" }).first()).toBeVisible();
  const detailUrl = page.url().split("?")[0];
  await page.getByLabel(t("workspaces.nameLabel")).fill("Renamed workspace");
  await page.getByRole("button", { name: t("settings.saveName"), exact: true }).click();
  await expect(page.getByRole("link", { name: "Renamed workspace" }).first()).toBeVisible();
  await page.getByLabel(t("settings.accountId")).fill("teammate");
  await page.getByRole("button", { name: t("settings.saveAccess"), exact: true }).click();
  await expect(page.getByRole("status")).toContainText(t("common.saved"));
  const blocked = await context.request.post(origin + "/workspaces/mutate", { headers: { Origin: origin }, form: { operation: "create", name: "CSRF attack", csrf: "wrong" } });
  expect(blocked.status()).toBe(403);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await expectNoHorizontalOverflow(page);
    await settleReveals(page);
    await page.screenshot({ path: `/tmp/nexora-workspace-${width}.png`, fullPage: true, animations: "disabled" });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await checkShell(page, context, detailUrl);
  await checkAgentRuns(page, provider, detailUrl);
  await checkGovernance(page, provider, detailUrl);
  await checkKnowledge(page, provider, detailUrl);
  await checkEvaluations(page, provider, detailUrl);
  await checkSpend(page, provider, detailUrl);
  await checkCatalogAndIntegrations(page, provider, detailUrl);
  const workspace = [...provider.workspaces.values()][0];
  // An admin manages the workspace name but never team access; a member manages neither.
  workspace.role = "admin";
  await page.goto(detailUrl);
  await expect(page.getByRole("button", { name: t("settings.saveName"), exact: true })).toHaveCount(1);
  await expect(page.getByText(t("settings.adminNotice"))).toBeVisible();
  await expect(page.getByRole("button", { name: t("settings.saveAccess"), exact: true })).toHaveCount(0);
  workspace.role = "member";
  await page.goto(detailUrl);
  await expect(page.getByText(t("settings.memberNotice")).first()).toBeVisible();
  await expect(page.getByRole("button", { name: t("settings.saveName"), exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: t("settings.saveAccess"), exact: true })).toHaveCount(0);
  await checkCatalogReadOnly(page, detailUrl);
  workspace.role = "owner";
  await checkPublicShell(page, origin);
  await checkLanguageNegotiation(context, origin);
  await page.goto(detailUrl);
  await page.getByRole("button", { name: t("navigation.signOut") }).click();
  await expect(page).toHaveURL(origin + "/login?status=signed_out");
  await context.addCookies([session!]);
  await page.goto(origin + "/workspaces");
  await expect(page).toHaveURL(origin + "/login");
  console.log("Browser flow passed: OIDC sign-in, create, rename, membership, CSRF, agent and run console, tool policy and approval decisions, knowledge ingestion, spend and budget console, integration vault and agent catalog, Turkish and English interface, light and dark themes, drawer navigation, 320px layouts, role UI and logout replay rejection");
} finally {
  await browser?.close(); child.kill("SIGTERM"); await provider.close();
}
