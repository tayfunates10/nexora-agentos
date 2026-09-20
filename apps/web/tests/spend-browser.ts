import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type { startProvider } from "./fixtures/provider.ts";

export async function checkSpend(
  page: Page,
  provider: Awaited<ReturnType<typeof startProvider>>,
  detailUrl: string,
) {
  const workspace = [...provider.workspaces.values()][0];
  const root = detailUrl + "/spend";
  await page.getByRole("link", { name: "Review spend and budget" }).click();
  await expect(page.getByRole("heading", { name: "Spend and budget" })).toBeVisible();
  await expect(page.getByText("No budget set", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "No priced model calls this period" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "No priced calls on this page" })).toBeVisible();

  // 26 agent-run calls at 2.5 units, one judge call at 0.5 and one embedding at 0.25.
  let newestId = "";
  for (let i = 0; i < 26; i++) {
    const id = randomUUID();
    if (i === 25) newestId = id;
    provider.spendRecords.set(id, {
      id, workspace_id: workspace.id, source_key: `agent-run:${randomUUID()}:step:${i}`,
      category: "agent_run", provider: "openai", model: "gpt-test",
      input_tokens: 1000, output_tokens: 200, cost_micros: 2_500_000,
      occurred_at: `2026-09-20T13:${String(i).padStart(2, "0")}:00Z`,
    });
  }
  const judgeRecordId = randomUUID();
  provider.spendRecords.set(judgeRecordId, {
    id: judgeRecordId, workspace_id: workspace.id,
    source_key: `eval-judge:${randomUUID()}:case:${randomUUID()}`,
    category: "evaluation_judge", provider: "openai", model: "judge-model-v1",
    input_tokens: 40, output_tokens: 20, cost_micros: 500_000,
    occurred_at: "2026-09-20T12:00:00Z",
  });

  const embeddingRecordId = randomUUID();
  provider.spendRecords.set(embeddingRecordId, {
    id: embeddingRecordId, workspace_id: workspace.id,
    source_key: `knowledge-source:${randomUUID()}`,
    category: "embedding", provider: "openai", model: "text-embedding-test",
    input_tokens: 240, output_tokens: 0, cost_micros: 250_000,
    occurred_at: "2026-09-20T11:00:00Z",
  });

  await page.reload();
  await expect(page.locator(".spend-panel")).toContainText("65.75");
  await expect(page.locator(".spend-panel")).toContainText("65,750,000 micros");
  const categories = page.locator(".spend-categories");
  await expect(categories.getByRole("row", { name: /Agent runs/ })).toContainText("26");
  await expect(categories.getByRole("row", { name: /Quality judge/ })).toContainText("0.50");
  // Embeddings are metered too, on input tokens only.
  await expect(categories.getByRole("row", { name: /Embeddings/ })).toContainText("0.25");
  await expect(page.getByText("No budget is set.", { exact: false })).toBeVisible();

  // The ledger paginates newest-first and the filter narrows it by category.
  const ledger = page.locator(".spend-ledger");
  await expect(ledger.getByRole("cell", { name: "openai/judge-model-v1" })).toHaveCount(0);
  await page.getByRole("link", { name: "Older calls" }).click();
  await expect(ledger.getByRole("cell", { name: "openai/judge-model-v1" })).toBeVisible();
  await page.getByRole("link", { name: "Newest calls" }).click();
  await page.getByRole("link", { name: "Embeddings", exact: true }).click();
  await expect(ledger.getByRole("cell", { name: "openai/text-embedding-test" })).toBeVisible();
  await expect(ledger.getByRole("cell", { name: "openai/gpt-test" })).toHaveCount(0);
  await page.getByRole("link", { name: "Quality judge", exact: true }).click();
  await expect(ledger.getByRole("cell", { name: "openai/judge-model-v1" })).toBeVisible();
  await expect(ledger.getByRole("cell", { name: "openai/gpt-test" })).toHaveCount(0);
  await page.getByRole("link", { name: "All", exact: true }).click();

  await page.getByLabel("Monthly limit (accounting units)").fill("100.25");
  await page.getByRole("button", { name: "Save budget" }).click();
  await expect(page.getByRole("status")).toContainText("budget was saved");
  expect(provider.budgets.get(workspace.id)).toEqual({
    monthly_limit_micros: 100_250_000, enforcement: "enforce",
  });
  await expect(page.getByText("Within budget", { exact: true })).toBeVisible();
  await expect(page.locator(".spend-panel")).toContainText("34.50");
  await expect(page.getByRole("meter")).toHaveAttribute("aria-valuenow", "65750000");

  // An amount the ledger cannot represent is refused in the browser…
  const limit = page.getByLabel("Monthly limit (accounting units)");
  await limit.fill("10.1234567");
  await page.getByRole("button", { name: "Save budget" }).click();
  expect(await limit.evaluate(node => (node as HTMLInputElement).validity.patternMismatch)).toBe(true);
  expect(provider.budgets.get(workspace.id)?.monthly_limit_micros).toBe(100_250_000);

  // …and again on the server, for a client that never ran the form validation.
  const csrf = await page.locator('input[name="csrf"]').first().inputValue();
  const origin = new URL(page.url()).origin;
  const rejected = await page.request.post(origin + "/workspaces/spend/budget", {
    headers: { Origin: origin, Referer: page.url() },
    form: { workspace: workspace.id, limit: "10.1234567", enforcement: "enforce", csrf },
    maxRedirects: 0,
  });
  expect(rejected.status()).toBe(303);
  expect(rejected.headers().location).toContain("budget_error=invalid");
  expect(provider.budgets.get(workspace.id)?.monthly_limit_micros).toBe(100_250_000);
  await page.goto(root + "?budget_error=invalid");
  await expect(page.locator("p[role=alert]")).toContainText("at most six decimals");

  await page.getByLabel("Monthly limit (accounting units)").fill("60");
  await page.getByRole("button", { name: "Save budget" }).click();
  await expect(page.getByText("Budget exhausted", { exact: true })).toBeVisible();
  await expect(page.locator("p[role=alert]")).toContainText("workspace_budget_exhausted");

  await page.getByLabel("Enforcement").selectOption("monitor");
  await page.getByRole("button", { name: "Save budget" }).click();
  await expect(page.getByText("Over limit · monitored, not blocked", { exact: true })).toBeVisible();
  await expect(page.getByText("no call is blocked", { exact: false })).toBeVisible();

  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/nexora-spend-${width}.png`, fullPage: true });
  }

  await page.goto(root + "?cursor=not-a-uuid");
  await expect(page.getByRole("heading", { name: "Invalid spend link" })).toBeVisible();
  await page.goto(root + "?category=fine-tuning");
  await expect(page.getByRole("heading", { name: "Invalid spend link" })).toBeVisible();
  await page.goto(root + "?cursor=" + newestId);
  await expect(page.getByRole("link", { name: "Newest calls" })).toBeVisible();

  provider.spendUnavailable(true);
  await page.goto(root);
  await expect(page.getByRole("heading", { name: "Spend unavailable" })).toBeVisible();
  provider.spendUnavailable(false);

  // Members may read workspace spend but never change the cap.
  workspace.role = "member";
  await page.goto(root);
  await expect(page.locator(".spend-panel")).toContainText("65.75");
  await expect(page.getByRole("button", { name: "Save budget" })).toHaveCount(0);
  await expect(page.getByText("Only owners and admins can change the budget.", { exact: false })).toBeVisible();
  const forbidden = await page.request.post(new URL("/workspaces/spend/budget", page.url()).href, {
    headers: { Origin: new URL(page.url()).origin, Referer: page.url() },
    form: { workspace: workspace.id, limit: "1", enforcement: "enforce", csrf: "wrong" },
  });
  expect(forbidden.status()).toBe(403);
  expect(provider.budgets.get(workspace.id)?.enforcement).toBe("monitor");

  workspace.role = "owner";
  await page.goto(detailUrl);
}
