import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type { startProvider } from "./fixtures/provider.ts";
import { t } from "./ui-text.ts";
import { expectNoHorizontalOverflow, settleReveals, sidebarLink } from "./shell.ts";

export async function checkKnowledge(
  page: Page,
  provider: Awaited<ReturnType<typeof startProvider>>,
  detailUrl: string,
) {
  const workspace = [...provider.workspaces.values()][0];
  await page.goto(detailUrl);
  await sidebarLink(page, t("navigation.knowledge")).click();
  await expect(page.getByRole("heading", { name: t("knowledge.title") })).toBeVisible();
  await expect(page.getByRole("heading", { name: t("knowledge.emptyTitle") })).toBeVisible();

  await page.getByLabel(t("knowledge.titleLabel")).fill("Operations handbook");
  await page.getByLabel(t("knowledge.keyLabel")).fill("handbook");
  await page.getByLabel(t("knowledge.versionLabel")).fill("v1");
  await page.getByLabel(t("knowledge.contentLabel")).fill("Escalate a paging incident to the on-call lead.");
  await page.getByRole("button", { name: t("knowledge.submit") }).click();

  // Queued, not indexed: the console says the worker still has to embed it.
  await expect(page.getByRole("status")).toContainText(t("knowledge.queued"));
  await expect(page.getByText(t("knowledge.ingestion.queued"))).toBeVisible();
  await expect(page.getByText(t("knowledge.ingestionHelp.queued"))).toBeVisible();
  const job = [...provider.ingestions.values()][0];
  expect(job.source_key).toBe("handbook");
  expect(page.url()).toContain(`job=${job.id}`);

  // A failed job names its code and never claims indexed content.
  job.status = "failed";
  job.error_code = "workspace_budget_exhausted";
  job.finished_at = "2026-09-20T17:01:00Z";
  await page.reload();
  await expect(page.getByTestId("ingestion-job").getByText(t("knowledge.ingestion.failed"))).toBeVisible();
  await expect(page.getByText("workspace_budget_exhausted")).toBeVisible();

  const source = {
    id: randomUUID(), workspace_id: workspace.id, source_key: "handbook", version: "v1",
    title: "Operations handbook", access_scope: "workspace" as const,
    content_hash: "a".repeat(64), chunk_count: 12,
    created_at: "2026-09-20T17:05:00Z", updated_at: "2026-09-20T17:05:00Z",
  };
  provider.sources.set(source.id, source);
  await page.goto(`${detailUrl}/knowledge`);
  await expect(page.getByRole("cell", { name: "Operations handbook" })).toBeVisible();
  await expect(page.getByRole("cell", { name: t("knowledge.scope.workspace") })).toBeVisible();

  await page.getByRole("button", { name: t("common.delete") }).click();
  await expect(page.getByRole("status")).toContainText(t("knowledge.deleted"));
  await expect(page.getByRole("heading", { name: t("knowledge.emptyTitle") })).toBeVisible();

  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${detailUrl}/knowledge`);
    await expect(page.getByRole("heading", { name: t("knowledge.addTitle") })).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await settleReveals(page);
    await page.screenshot({ path: `/tmp/nexora-knowledge-${width}.png`, fullPage: true, animations: "disabled" });
  }
  await page.setViewportSize({ width: 1440, height: 900 });

  // A member reads the list and cannot change it.
  provider.sources.set(source.id, source);
  workspace.role = "member";
  await page.goto(`${detailUrl}/knowledge`);
  await expect(page.getByText(t("knowledge.readOnlyNotice"))).toBeVisible();
  await expect(page.getByRole("button", { name: t("common.delete") })).toHaveCount(0);
  await expect(page.getByRole("button", { name: t("knowledge.submit") })).toHaveCount(0);
  await expect(page.getByRole("cell", { name: "Operations handbook" })).toBeVisible();
  workspace.role = "owner";
  await page.goto(detailUrl);
}
