import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type { startProvider } from "./fixtures/provider.ts";
import { t } from "./ui-text.ts";
import { expectNoHorizontalOverflow, settleReveals, sidebarLink } from "./shell.ts";

export async function checkSpend(
  page: Page,
  provider: Awaited<ReturnType<typeof startProvider>>,
  detailUrl: string,
) {
  const workspace = [...provider.workspaces.values()][0];
  const root = detailUrl + "/spend";
  await sidebarLink(page, t("navigation.spend")).click();
  await expect(page.getByRole("heading", { name: t("spend.title") })).toBeVisible();
  await expect(page.getByText(t("spend.state.noBudget"), { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: t("spend.categoriesEmptyTitle") })).toBeVisible();
  await expect(page.getByRole("heading", { name: t("spend.ledgerEmptyTitle") })).toBeVisible();

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
  await expect(page.getByTestId("spend-summary")).toContainText("65,75");
  await expect(page.getByTestId("spend-summary"))
    .toContainText(t("format.microsUnit", { value: "65.750.000" }));
  const categories = page.getByTestId("spend-categories");
  await expect(categories.getByRole("row", { name: new RegExp(t("spend.category.agent_run")) }))
    .toContainText("26");
  await expect(categories.getByRole("row", {
    name: new RegExp(t("spend.category.evaluation_judge")),
  })).toContainText("0,50");
  // Embeddings are metered too, on input tokens only.
  await expect(categories.getByRole("row", { name: new RegExp(t("spend.category.embedding")) }))
    .toContainText("0,25");
  await expect(page.getByText(t("spend.noBudgetNotice"))).toBeVisible();

  // The ledger paginates newest-first and the filter narrows it by category.
  const ledger = page.getByTestId("spend-ledger");
  await expect(ledger.getByRole("cell", { name: "openai/judge-model-v1" })).toHaveCount(0);
  await page.getByRole("link", { name: t("spend.olderCalls") }).click();
  await expect(ledger.getByRole("cell", { name: "openai/judge-model-v1" })).toBeVisible();
  await page.getByRole("link", { name: t("spend.newestCalls") }).click();
  await page.getByRole("link", { name: t("spend.category.embedding"), exact: true }).click();
  await expect(ledger.getByRole("cell", { name: "openai/text-embedding-test" })).toBeVisible();
  await expect(ledger.getByRole("cell", { name: "openai/gpt-test" })).toHaveCount(0);
  await page.getByRole("link", { name: t("spend.category.evaluation_judge"), exact: true }).click();
  await expect(ledger.getByRole("cell", { name: "openai/judge-model-v1" })).toBeVisible();
  await expect(ledger.getByRole("cell", { name: "openai/gpt-test" })).toHaveCount(0);
  await page.getByRole("link", { name: t("common.all"), exact: true }).click();

  await page.getByLabel(t("spend.limitLabel")).fill("100,25");
  await page.getByLabel(t("spend.thresholdOption", { percent: "%50" })).check();
  await page.getByLabel(t("spend.thresholdOption", { percent: "%90" })).check();
  await page.getByRole("button", { name: t("spend.saveBudget") }).click();
  await expect(page.getByRole("status")).toContainText(t("spend.saved"));
  expect(provider.budgets.get(workspace.id)).toEqual({
    monthly_limit_micros: 100_250_000, enforcement: "enforce", alert_thresholds: [50, 90],
  });
  // 65.75 of 100.25 units is past 50% but not 90%.
  await expect(page.getByTestId("spend-alerts")).toContainText(t("spend.alertEntry", {
    percent: "%50", consumed: "65,75", limit: "100,25",
  }));
  await expect(page.getByTestId("spend-alerts")).not.toContainText("%90");
  await expect(page.getByText(t("spend.state.withinBudget"), { exact: true })).toBeVisible();
  await expect(page.getByTestId("spend-summary")).toContainText("34,50");
  await expect(page.getByRole("meter")).toHaveAttribute("aria-valuenow", "65750000");

  // An amount the ledger cannot represent is refused in the browser…
  const limit = page.getByLabel(t("spend.limitLabel"));
  await limit.fill("10,1234567");
  await page.getByRole("button", { name: t("spend.saveBudget") }).click();
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
  await expect(page.locator("p[role=alert]")).toContainText(t("spend.error.invalid", {
    min: "0", max: "1.000.000.000", decimals: "6",
  }));

  await page.getByLabel(t("spend.limitLabel")).fill("60");
  await page.getByRole("button", { name: t("spend.saveBudget") }).click();
  await expect(page.getByText(t("spend.state.exhausted"), { exact: true })).toBeVisible();
  await expect(page.locator("p[role=alert]")).toContainText(t("spend.exhaustedNotice"));
  // A tighter limit crosses 90% without another call, and the 50% alert keeps the
  // amount it was raised at rather than being rewritten.
  await expect(page.getByTestId("spend-alerts")).toContainText(t("spend.alertEntry", {
    percent: "%90", consumed: "65,75", limit: "60,00",
  }));
  await expect(page.getByTestId("spend-alerts")).toContainText(t("spend.alertEntry", {
    percent: "%50", consumed: "65,75", limit: "100,25",
  }));

  await page.getByLabel(t("spend.enforcementLabel")).selectOption("monitor");
  await page.getByRole("button", { name: t("spend.saveBudget") }).click();
  // Thresholds already reached are not raised a second time.
  expect([...provider.spendAlerts.keys()].filter(key => key.startsWith(workspace.id))).toHaveLength(2);
  await expect(page.getByText(t("spend.state.overLimitMonitored"), { exact: true })).toBeVisible();
  await expect(page.getByText(t("spend.monitorNotice"))).toBeVisible();

  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await expectNoHorizontalOverflow(page);
    await settleReveals(page);
    await page.screenshot({ path: `/tmp/nexora-spend-${width}.png`, fullPage: true, animations: "disabled" });
  }

  await page.goto(root + "?cursor=not-a-uuid");
  await expect(page.getByRole("heading", { name: t("common.invalidLinkTitle") })).toBeVisible();
  await page.goto(root + "?category=fine-tuning");
  await expect(page.getByRole("heading", { name: t("common.invalidLinkTitle") })).toBeVisible();
  await page.goto(root + "?cursor=" + newestId);
  await expect(page.getByRole("link", { name: t("spend.newestCalls") })).toBeVisible();

  // Clearing every threshold is allowed, and the console says what that means.
  await page.getByLabel(t("spend.thresholdOption", { percent: "%50" })).uncheck();
  await page.getByLabel(t("spend.thresholdOption", { percent: "%90" })).uncheck();
  await page.getByRole("button", { name: t("spend.saveBudget") }).click();
  expect(provider.budgets.get(workspace.id)?.alert_thresholds).toEqual([]);
  await expect(page.getByText(t("spend.noThresholdsNotice"))).toBeVisible();
  await page.getByLabel(t("spend.thresholdOption", { percent: "%90" })).check();
  await page.getByRole("button", { name: t("spend.saveBudget") }).click();
  expect(provider.budgets.get(workspace.id)?.alert_thresholds).toEqual([90]);

  provider.spendUnavailable(true);
  await page.goto(root);
  await expect(page.getByRole("heading", {
    name: t("errors.unavailable", { area: t("spend.area") }),
  })).toBeVisible();
  provider.spendUnavailable(false);

  // Members may read workspace spend but never change the cap.
  workspace.role = "member";
  await page.goto(root);
  await expect(page.getByTestId("spend-summary")).toContainText("65,75");
  await expect(page.getByRole("button", { name: t("spend.saveBudget") })).toHaveCount(0);
  await expect(page.getByText(t("spend.readOnlyNotice"))).toBeVisible();
  const forbidden = await page.request.post(new URL("/workspaces/spend/budget", page.url()).href, {
    headers: { Origin: new URL(page.url()).origin, Referer: page.url() },
    form: { workspace: workspace.id, limit: "1", enforcement: "enforce", csrf: "wrong" },
  });
  expect(forbidden.status()).toBe(403);
  expect(provider.budgets.get(workspace.id)?.enforcement).toBe("monitor");

  workspace.role = "owner";
  await page.goto(detailUrl);
}
