import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type { startProvider } from "./fixtures/provider.ts";

export async function checkKnowledge(
  page: Page,
  provider: Awaited<ReturnType<typeof startProvider>>,
  detailUrl: string,
) {
  const workspace = [...provider.workspaces.values()][0];
  await page.goto(detailUrl);
  await page.getByRole("link", { name: "Knowledge sources →" }).click();
  await expect(page.getByRole("heading", { name: "Knowledge sources" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "No indexed sources on this page" })).toBeVisible();

  await page.getByLabel("Title").fill("Operations handbook");
  await page.getByLabel("Source key").fill("handbook");
  await page.getByLabel("Version").fill("v1");
  await page.getByLabel("Content").fill("Escalate a paging incident to the on-call lead.");
  await page.getByRole("button", { name: "Queue ingestion" }).click();

  // Queued, not indexed: the console says the worker still has to embed it.
  await expect(page.getByRole("status")).toContainText("queued for ingestion");
  await expect(page.getByText("Queued for the worker")).toBeVisible();
  await expect(page.getByText("nothing is sent to a provider until then", { exact: false })).toBeVisible();
  const job = [...provider.ingestions.values()][0];
  expect(job.source_key).toBe("handbook");
  expect(page.url()).toContain(`job=${job.id}`);

  // A failed job names its code and never claims indexed content.
  job.status = "failed";
  job.error_code = "workspace_budget_exhausted";
  job.finished_at = "2026-09-20T17:01:00Z";
  await page.reload();
  await expect(page.getByText("Failed", { exact: true })).toBeVisible();
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
  await expect(page.getByRole("cell", { name: "Everyone in this workspace" })).toBeVisible();

  await page.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("status")).toContainText("The source was deleted");
  await expect(page.getByRole("heading", { name: "No indexed sources on this page" })).toBeVisible();

  // A member reads the list and cannot change it.
  provider.sources.set(source.id, source);
  workspace.role = "member";
  await page.goto(`${detailUrl}/knowledge`);
  await expect(page.getByText("You have read-only access to knowledge sources.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Delete" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Queue ingestion" })).toHaveCount(0);
  await expect(page.getByRole("cell", { name: "Operations handbook" })).toBeVisible();
  workspace.role = "owner";
  await page.goto(detailUrl);
}
