import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type { startProvider } from "./fixtures/provider.ts";
import { t } from "./ui-text.ts";
import { settleReveals, sidebarLink } from "./shell.ts";

export async function checkAgentRuns(
  page: Page,
  provider: Awaited<ReturnType<typeof startProvider>>,
  detailUrl: string,
) {
  const workspace = [...provider.workspaces.values()][0];
  await page.goto(detailUrl);
  await sidebarLink(page, t("navigation.agents")).click();
  await expect(page.getByRole("heading", { name: t("agents.title"), exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: t("agents.emptyTitle") })).toBeVisible();

  await page.getByLabel(t("agents.nameLabel")).fill("Research agent");
  await page.getByLabel(t("agents.instructionsLabel")).fill("Answer with citations from workspace knowledge.");
  await page.getByLabel(t("agents.profileLabel")).fill("balanced-v1");
  await page.getByRole("button", { name: t("agents.createSubmit") }).click();
  await expect(page.getByRole("status")).toContainText(t("agents.created"));
  await expect(page.getByRole("heading", { name: "Research agent" })).toBeVisible();
  await expect(page.getByText(t("agents.profile", { profile: "balanced-v1" }))).toBeVisible();

  // Starting a run is a member right, so the form is on the agent itself.
  await page.getByText(t("agents.startWith", { agentName: "Research agent" })).click();
  await page.getByLabel(t("agents.taskLabel")).fill("Summarise this week's incidents.");
  await page.getByRole("button", { name: t("agents.startRun") }).click();
  await expect(page.getByRole("heading", { name: t("runDetail.title") })).toBeVisible();
  const runUrl = page.url();
  const run = [...provider.runs.values()][0];
  expect(runUrl).toBe(`${detailUrl}/runs/${run.id}`);
  await expect(page.getByTestId("run-summary").locator(".status").first()).toHaveText(t("runs.status.queued"));
  await expect(page.getByText(t("runs.statusHelp.queued"))).toBeVisible();
  await expect(page.getByRole("heading", { name: t("runDetail.result.pendingTitle") })).toBeVisible();
  await expect(page.getByRole("listitem").filter({ hasText: t("runEvent.run.queued") })).toBeVisible();

  // A queued run is cancellable, and cancelling publishes no partial answer.
  await page.getByRole("button", { name: t("runDetail.cancelSubmit") }).click();
  await expect(page.getByRole("status")).toContainText(t("runDetail.cancelRecorded"));
  await expect(page.getByTestId("run-summary").locator(".status").first()).toHaveText(t("runs.status.cancelled"));
  await expect(page.getByText(t("runDetail.result.noOutput"))).toBeVisible();
  await expect(page.getByRole("button", { name: t("runDetail.cancelSubmit") })).toHaveCount(0);

  // A successful run shows the recorded answer and its model steps to its requester.
  const succeeded = {
    id: randomUUID(), workspace_id: workspace.id, agent_id: run.agent_id,
    agent_name: run.agent_name, trace_id: randomUUID(), status: "succeeded", attempt_count: 1,
    requested_by_me: true, cancel_requested_at: null,
    finished_at: "2026-09-20T10:02:00Z", failure_code: null,
    created_at: "2026-09-20T10:00:00Z", updated_at: "2026-09-20T10:02:00Z",
  };
  provider.runs.set(succeeded.id, succeeded);
  provider.runEvents.set(succeeded.id, [
    {
      id: randomUUID(), event_no: 1, event_type: "run.queued",
      payload: { request_id: "fixture-request" }, created_at: "2026-09-20T10:00:00Z",
    },
    {
      id: randomUUID(), event_no: 2, event_type: "model.completed",
      payload: { step_no: 0, provider: "openai" }, created_at: "2026-09-20T10:01:00Z",
    },
    {
      id: randomUUID(), event_no: 3, event_type: "run.succeeded",
      payload: { attempt: 1 }, created_at: "2026-09-20T10:02:00Z",
    },
  ]);
  provider.runActions.set(succeeded.id, [{
    id: randomUUID(), workspace_id: workspace.id, run_id: succeeded.id,
    action_name: "search_incidents", side_effect: "read", status: "succeeded",
    policy_decision: "allow", attempt_count: 1, error_code: null,
    approval_id: null, approval_status: null,
    created_at: "2026-09-20T10:00:30Z", updated_at: "2026-09-20T10:01:00Z",
    started_at: "2026-09-20T10:00:30Z", finished_at: "2026-09-20T10:01:00Z",
  }]);
  provider.runResults.set(succeeded.id, {
    run_id: succeeded.id, workspace_id: workspace.id, agent_id: run.agent_id,
    trace_id: succeeded.trace_id, status: "succeeded",
    output_text: "Two incidents were recorded this week.", finish_reason: "stop",
    failure_code: null, recorded_input_tokens: 1200, recorded_output_tokens: 180,
    selected_tools: ["search_incidents"],
    model_steps: [{
      step_no: 0, provider: "openai", model: "gpt-test", finish_reason: "stop",
      input_tokens: 1200, output_tokens: 180,
    }],
  });
  await page.goto(`${detailUrl}/runs/${succeeded.id}`);
  await expect(page.getByText("Two incidents were recorded this week.")).toBeVisible();
  await expect(page.getByText(t("runEvent.model.completed"))).toBeVisible();
  await expect(page.getByRole("heading", { name: t("runDetail.actions.title") })).toBeVisible();
  await expect(
    page.getByRole("cell", { name: t("runDetail.actions.status.succeeded"), exact: true }),
  ).toBeVisible();
  await expect(page.getByText("search_incidents").first()).toBeVisible();
  await expect(page.getByText(t("runDetail.result.selectionNotice", { tools: "search_incidents" }))).toBeVisible();

  // The answer belongs to the requester: admin access does not open it.
  succeeded.requested_by_me = false;
  await page.reload();
  await expect(page.getByRole("heading", { name: t("runDetail.result.notRequesterTitle") })).toBeVisible();
  await expect(page.getByText("Two incidents were recorded this week.")).toHaveCount(0);
  succeeded.requested_by_me = true;

  // History lists both runs, filters by status and by who started them.
  await page.goto(detailUrl);
  await sidebarLink(page, t("navigation.runs")).click();
  await expect(page.getByRole("heading", { name: t("runs.title") })).toBeVisible();
  await expect(page.getByRole("row")).toHaveCount(3);
  await page.getByRole("link", { name: t("runs.status.cancelled"), exact: true }).click();
  await expect(page.getByRole("row")).toHaveCount(2);
  await expect(page.getByRole("cell", { name: "Research agent" })).toBeVisible();
  await page.getByRole("link", { name: t("runs.allStatuses") }).click();
  await page.getByRole("link", { name: t("runs.startedByMe"), exact: true }).click();
  await expect(page.getByRole("row")).toHaveCount(3);
  await page.goto(`${detailUrl}/runs?status=nonsense`);
  await expect(page.getByRole("heading", { name: t("common.invalidLinkTitle") })).toBeVisible();

  // A member keeps the run rights they have and loses agent definition management.
  workspace.role = "member";
  await page.goto(`${detailUrl}/agents`);
  await expect(page.getByText(t("agents.readOnlyNotice"))).toBeVisible();
  await expect(page.getByRole("button", { name: t("agents.createSubmit") })).toHaveCount(0);
  await expect(page.getByText(t("agents.startWith", { agentName: "Research agent" }))).toBeVisible();
  workspace.role = "owner";

  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${detailUrl}/runs/${succeeded.id}`);
    // Measure the rendered run, not the loading state it replaces.
    await expect(page.getByRole("heading", { name: t("runDetail.timelineTitle") })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await settleReveals(page);
    await page.screenshot({ path: `/tmp/nexora-run-${width}.png`, fullPage: true, animations: "disabled" });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(detailUrl);
}
