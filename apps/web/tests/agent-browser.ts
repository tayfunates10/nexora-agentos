import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type { startProvider } from "./fixtures/provider.ts";

export async function checkAgentRuns(
  page: Page,
  provider: Awaited<ReturnType<typeof startProvider>>,
  detailUrl: string,
) {
  const workspace = [...provider.workspaces.values()][0];
  await page.goto(detailUrl);
  await page.getByRole("link", { name: "Agents →" }).click();
  await expect(page.getByRole("heading", { name: "Agents", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "No agents on this page" })).toBeVisible();

  await page.getByLabel("Agent name").fill("Research agent");
  await page.getByLabel("Instructions").fill("Answer with citations from workspace knowledge.");
  await page.getByLabel("Model profile").fill("balanced-v1");
  await page.getByRole("button", { name: "Create agent" }).click();
  await expect(page.getByRole("status")).toContainText("The agent was created");
  await expect(page.getByRole("heading", { name: "Research agent" })).toBeVisible();
  await expect(page.getByText("PROFILE BALANCED-V1")).toBeVisible();

  // Starting a run is a member right, so the form is on the agent itself.
  await page.getByText("Start a run with Research agent").click();
  await page.getByLabel("What should this agent do?").fill("Summarise this week's incidents.");
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page.getByRole("heading", { name: "Agent run" })).toBeVisible();
  const runUrl = page.url();
  const run = [...provider.runs.values()][0];
  expect(runUrl).toBe(`${detailUrl}/runs/${run.id}`);
  await expect(page.locator(".run-panel .state")).toHaveText("Queued");
  await expect(page.getByText("Waiting for a worker.", { exact: false })).toBeVisible();
  await expect(page.getByRole("heading", { name: "No result yet" })).toBeVisible();
  await expect(page.getByRole("listitem").filter({ hasText: "Queued" })).toBeVisible();

  // A queued run is cancellable, and cancelling publishes no partial answer.
  await page.getByRole("button", { name: "Cancel this run" }).click();
  await expect(page.getByRole("status")).toContainText("Cancellation was recorded");
  await expect(page.locator(".run-panel .state")).toHaveText("Cancelled");
  await expect(page.getByText("This run published no answer.", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel this run" })).toHaveCount(0);

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
  await expect(page.getByText("Model step recorded")).toBeVisible();
  await expect(page.getByText("search_incidents")).toBeVisible();
  await expect(page.getByText("Selection is not execution", { exact: false })).toBeVisible();

  // The answer belongs to the requester: admin access does not open it.
  succeeded.requested_by_me = false;
  await page.reload();
  await expect(page.getByRole("heading", { name: "Result not available to you" })).toBeVisible();
  await expect(page.getByText("Two incidents were recorded this week.")).toHaveCount(0);
  succeeded.requested_by_me = true;

  // History lists both runs, filters by status and by who started them.
  await page.goto(detailUrl);
  await page.getByRole("link", { name: "Agent runs →" }).click();
  await expect(page.getByRole("heading", { name: "Agent runs" })).toBeVisible();
  await expect(page.getByRole("row")).toHaveCount(3);
  await page.getByRole("link", { name: "Cancelled", exact: true }).click();
  await expect(page.getByRole("row")).toHaveCount(2);
  await expect(page.getByRole("cell", { name: "Research agent" })).toBeVisible();
  await page.getByRole("link", { name: "All statuses" }).click();
  await page.getByRole("link", { name: "Started by me", exact: true }).click();
  await expect(page.getByRole("row")).toHaveCount(3);
  await page.goto(`${detailUrl}/runs?status=nonsense`);
  await expect(page.getByRole("heading", { name: "Invalid link" })).toBeVisible();

  // A member keeps the run rights they have and loses agent definition management.
  workspace.role = "member";
  await page.goto(`${detailUrl}/agents`);
  await expect(page.getByText("You have read-only access to agent definitions.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Create agent" })).toHaveCount(0);
  await expect(page.getByText("Start a run with Research agent")).toBeVisible();
  workspace.role = "owner";

  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${detailUrl}/runs/${succeeded.id}`);
    // Measure the rendered run, not the loading state it replaces.
    await expect(page.getByRole("heading", { name: "Execution timeline" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.screenshot({ path: `/tmp/nexora-run-${width}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(detailUrl);
}
