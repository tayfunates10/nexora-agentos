import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type { startProvider } from "./fixtures/provider.ts";

export async function checkGovernance(
  page: Page,
  provider: Awaited<ReturnType<typeof startProvider>>,
  detailUrl: string,
) {
  const workspace = [...provider.workspaces.values()][0];
  const run = [...provider.runs.values()][0];

  await page.goto(detailUrl);
  await page.getByRole("link", { name: "Tools and policy →" }).click();
  await expect(page.getByRole("heading", { name: "Tools and policy" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "No tools registered" })).toBeVisible();

  // A registered contract with no policy row is denied by default, and says so.
  const unpoliced = {
    id: randomUUID(), workspace_id: workspace.id, name: "search-handbook",
    server_key: "ops", remote_name: "search", description: "Search the operations handbook.",
    input_schema: { type: "object", properties: { query: { type: "string" } } },
    output_schema: null, side_effect: "read" as const, enabled: true,
    policy_decision: null, policy_reason: null, policy_updated_at: null,
    created_at: "2026-09-20T09:00:00Z", updated_at: "2026-09-20T09:00:00Z",
  };
  const destructive = {
    ...unpoliced, id: randomUUID(), name: "delete-record", remote_name: "delete",
    description: "Delete an archived record.", side_effect: "destructive" as const,
    policy_decision: "allow" as const, policy_reason: "Allowed for the operations team.",
    policy_updated_at: "2026-09-20T10:00:00Z",
  };
  provider.tools.set(unpoliced.id, unpoliced);
  provider.tools.set(destructive.id, destructive);
  await page.reload();
  await expect(page.getByText("No policy · denied by default")).toBeVisible();
  await expect(page.getByText("Allow does not remove the human step", { exact: false })).toBeVisible();

  await page.getByLabel("Policy decision").first().selectOption("require_approval");
  await page.getByLabel("Reason").first().fill("Handbook search needs review while we pilot it.");
  await page.getByRole("button", { name: "Save policy" }).first().click();
  await expect(page.getByRole("status")).toContainText("The policy was saved");
  await expect(page.getByText("Handbook search needs review while we pilot it.")).toBeVisible();

  // The approval itself: a held call, its exact arguments, and a decision that closes it.
  await page.goto(detailUrl);
  await page.getByRole("link", { name: "Tool approvals →" }).click();
  await expect(page.getByRole("heading", { name: "Nothing is waiting for a decision" })).toBeVisible();

  const approval = {
    id: randomUUID(), workspace_id: workspace.id, run_id: run.id, tool_call_id: randomUUID(),
    requested_action: "delete-record",
    normalized_arguments: { record_id: "R-4471", idempotency_key: "delete-r4471-0001" },
    requester_subject: "fixture-alice", approver_subject: null, status: "pending" as const,
    policy_reason: "Destructive tools always require a human decision.",
    expires_at: new Date(Date.now() + 900_000).toISOString(),
    created_at: "2026-09-20T15:50:00Z", decided_at: null,
  };
  provider.approvals.set(approval.id, approval);
  await page.reload();
  await expect(page.getByRole("heading", { name: "delete-record" })).toBeVisible();
  await expect(page.getByText("Destructive tools always require a human decision.")).toBeVisible();
  await expect(page.getByText("\"record_id\": \"R-4471\"")).toBeVisible();
  await expect(page.getByRole("link", { name: approval.run_id })).toBeVisible();

  await page.getByRole("button", { name: "Reject" }).click();
  await expect(page.getByRole("status")).toContainText("The call was rejected");
  await expect(page.getByRole("heading", { name: "Nothing is waiting for a decision" })).toBeVisible();
  await page.getByRole("link", { name: "Rejected", exact: true }).click();
  await expect(page.getByRole("heading", { name: "delete-record" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Reject" })).toHaveCount(0);
  await expect(page.getByText("This request is closed.", { exact: false })).toBeVisible();

  // Deciding twice is refused by the API, and the console says why instead of failing blankly.
  const origin = new URL(page.url()).origin;
  const stale = await page.request.post(origin + "/workspaces/approvals/decide", {
    headers: { Origin: origin },
    form: {
      csrf: await page.locator("input[name=csrf]").first().inputValue(),
      workspace: workspace.id, approval: approval.id, decision: "approved",
    },
    maxRedirects: 0,
  });
  expect(stale.status()).toBe(303);
  expect(stale.headers()["location"]).toContain("error=gone");
  await page.goto(stale.headers()["location"]);
  await expect(page.getByText("That approval is no longer open.", { exact: false })).toBeVisible();

  // The decision surface has to be usable on a phone: an approver is often not at a desk.
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${detailUrl}/approvals?status=rejected`);
    await expect(page.getByRole("heading", { name: "delete-record" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/nexora-approvals-${width}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 900 });

  // A member may not read approvals at all, and may not change a tool policy.
  workspace.role = "member";
  await page.goto(`${detailUrl}/approvals`);
  await expect(page.getByRole("heading", { name: "Approvals not found or access denied" })).toBeVisible();
  await page.goto(`${detailUrl}/tools`);
  await expect(page.getByText("Only owners and admins can change a tool policy.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save policy" })).toHaveCount(0);
  workspace.role = "owner";
  await page.goto(detailUrl);
}
