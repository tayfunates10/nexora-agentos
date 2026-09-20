import { chromium, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { startProvider } from "./fixtures/provider.ts";
import { checkEvaluations } from "./evaluation-browser.ts";
import { checkSpend } from "./spend-browser.ts";

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
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext(); const page = await context.newPage();
  await page.goto(origin + "/workspaces");
  await expect(page).toHaveURL(origin + "/login");
  await page.getByRole("button", { name: "Continue to sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your workspaces." })).toBeVisible();
  await expect(page.getByRole("heading", { name: "No workspaces yet" })).toBeVisible();
  const cookies = await context.cookies();
  const session = cookies.find(cookie => cookie.name === "nexora_session");
  expect(session?.httpOnly).toBe(true); expect(session?.sameSite).toBe("Lax");
  expect(session?.value).toMatch(/^[A-Za-z0-9_-]{43}$/);
  await page.getByLabel("Workspace name").fill("Browser workspace");
  await page.getByRole("button", { name: "Create workspace" }).click();
  await expect(page.getByRole("heading", { name: "Browser workspace" })).toBeVisible();
  const detailUrl = page.url().split("?")[0];
  await page.getByLabel("Workspace name").fill("Renamed workspace");
  await page.getByRole("button", { name: "Save name" }).click();
  await expect(page.getByRole("heading", { name: "Renamed workspace" })).toBeVisible();
  await page.getByLabel("Account ID").fill("teammate");
  await page.getByRole("button", { name: "Save access" }).click();
  await expect(page.getByRole("status")).toContainText("Your changes were saved");
  const blocked = await context.request.post(origin + "/workspaces/mutate", { headers: { Origin: origin }, form: { operation: "create", name: "CSRF attack", csrf: "wrong" } });
  expect(blocked.status()).toBe(403);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/nexora-workspace-${width}.png`, fullPage: true });
  }
  await checkEvaluations(page, provider, detailUrl);
  await checkSpend(page, provider, detailUrl);
  const workspace = [...provider.workspaces.values()][0]; workspace.role = "member";
  await page.goto(detailUrl);
  await expect(page.getByText("You have read-only access.", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save name" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save access" })).toHaveCount(0);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(origin + "/login?status=signed_out");
  await context.addCookies([session!]);
  await page.goto(origin + "/workspaces");
  await expect(page).toHaveURL(origin + "/login");
  console.log("Browser flow passed: OIDC sign-in, create, rename, membership, CSRF, spend and budget console, responsive layouts, role UI and logout replay rejection");
} finally {
  await browser?.close(); child.kill("SIGTERM"); await provider.close();
}
