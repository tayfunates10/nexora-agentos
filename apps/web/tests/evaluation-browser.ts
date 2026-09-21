import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import type { startProvider } from "./fixtures/provider.ts";
import { t } from "./ui-text.ts";
import { expectNoHorizontalOverflow, settleReveals, sidebarLink } from "./shell.ts";

export async function checkEvaluations(page: Page, provider: Awaited<ReturnType<typeof startProvider>>, detailUrl: string) {
  const workspace = [...provider.workspaces.values()][0];
  const root = detailUrl + "/evaluations";
  await sidebarLink(page, t("navigation.evaluations")).click();
  await expect(page.getByRole("heading", { name: t("evaluations.emptyTitle") })).toBeVisible();
  const suiteId = randomUUID(), firstCase = randomUUID(), secondCase = randomUUID();
  provider.evalSuites.set(suiteId, {
    id: suiteId, workspace_id: workspace.id, name: "Support quality", version: 2,
    description: "Grounded answers without unsafe tools.", case_count: 2,
    created_at: "2026-09-20T12:00:00Z", cases: [
      { id: firstCase, case_no: 1, case_key: "grounded-read", input: "Read the handbook.",
        expected_tools: ["search"], forbidden_tools: ["delete"], expected_citations: ["handbook:v1"] },
      { id: secondCase, case_no: 2, case_key: "safe-action", input: "Inspect the record.",
        expected_tools: [], forbidden_tools: ["delete"], expected_citations: [] },
    ],
  });
  await page.reload();
  await page.getByRole("link", { name: "Support quality" }).click();
  await expect(page.getByRole("heading", { name: t("evaluations.historyEmptyTitle") })).toBeVisible();
  const baselineId = randomUUID();
  const failedOutput = "  <script>window.evaluationInjected = true</script>\n" + "long-evidence-".repeat(80);
  let candidateId = "";
  for (let i = 0; i < 26; i++) {
    const id = i === 0 ? baselineId : randomUUID();
    if (i === 25) candidateId = id;
    const candidate = i === 25;
    provider.evalRuns.set(id, {
      id, workspace_id: workspace.id, suite_id: suiteId,
      candidate_label: candidate ? "Candidate v2" : i === 0 ? "Baseline v1" : "Historical run " + i,
      baseline_eval_run_id: candidate ? baselineId : null, case_count: 2,
      passed_count: 1, failed_count: 1, regression_count: candidate ? 1 : 0,
      improvement_count: candidate ? 1 : 0, created_at: `2026-09-20T13:${String(i).padStart(2, "0")}:00Z`,
      results: [
        { case_id: firstCase, case_key: "grounded-read", passed: !candidate,
          failures: candidate ? ["missing_citation:handbook:v1"] : [], selected_tools: ["search"],
          citations: candidate ? [] : ["handbook:v1"], raw_output: candidate ? failedOutput : null,
          source_agent_run_id: randomUUID(), baseline_passed: candidate ? true : null,
          regression: candidate, improvement: false },
        { case_id: secondCase, case_key: "safe-action", passed: candidate,
          failures: candidate ? [] : ["forbidden_tool:delete"], selected_tools: candidate ? [] : ["delete"],
          citations: [], raw_output: candidate ? null : "Unsafe delete",
          source_agent_run_id: randomUUID(), baseline_passed: candidate ? false : null,
          regression: false, improvement: candidate },
      ],
    });
  }
  await page.reload();
  await page.getByRole("link", { name: t("evaluations.olderResults") }).click();
  await expect(page.getByRole("link", { name: "Baseline v1", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: t("evaluations.olderResults") })).toHaveCount(0);
  await page.getByRole("link", { name: t("evaluations.newestResults") }).click();
  await page.getByRole("link", { name: "Candidate v2", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Candidate v2" })).toBeVisible();
  await expect(page.getByText(
    `${t("evaluations.caseFailed")} · ${t("evaluations.caseRegression")}`, { exact: true },
  )).toBeVisible();
  await expect(page.getByText(
    `${t("evaluations.casePassed")} · ${t("evaluations.caseImprovement")}`, { exact: true },
  )).toBeVisible();
  await page.getByText(t("evaluations.failedOutput"), { exact: true }).click();
  await expect(page.locator(".codeblock").last()).toHaveText(failedOutput);
  expect(await page.evaluate(() => "evaluationInjected" in window)).toBe(false);

  await page.getByRole("button", { name: t("judge.run") }).click();
  await expect(page.getByRole("status")).toContainText(t("judge.queued"));
  await expect(page.getByTestId("judge-panel")).toContainText(t("judge.status.queued"));
  const queuedJudge = [...provider.evalJudgeRuns.values()].find(
    item => item.eval_run_id === candidateId,
  );
  expect(queuedJudge).toBeTruthy();
  provider.evalJudgeRuns.set(queuedJudge!.id, {
    ...queuedJudge!,
    status: "succeeded",
    scored_count: 2,
    judge_provider: "openai",
    judge_model: "judge-model-v1",
    prompt_version: "nexora-eval-judge-v1",
    quality_milli: 875,
    baseline_quality_milli: 750,
    quality_delta_milli: 125,
    regression_count: 0,
    improvement_count: 1,
    input_tokens: 48,
    output_tokens: 16,
    latency_ms: 123,
    finished_at: "2026-09-20T14:01:00Z",
    results: [
      {
        case_id: firstCase, case_key: "grounded-read", task_completion: 4,
        answer_relevance: 4, clarity: 4, quality_milli: 1000,
        rationale: "Strong grounded answer.", baseline_task_completion: 3,
        baseline_answer_relevance: 3, baseline_clarity: 3, baseline_quality_milli: 750,
        quality_delta_milli: 250, regression: false, improvement: true,
        input_tokens: 24, output_tokens: 8, latency_ms: 70,
      },
      {
        case_id: secondCase, case_key: "safe-action", task_completion: 3,
        answer_relevance: 3, clarity: 3, quality_milli: 750,
        rationale: "Clear and relevant.", baseline_task_completion: 3,
        baseline_answer_relevance: 3, baseline_clarity: 3, baseline_quality_milli: 750,
        quality_delta_milli: 0, regression: false, improvement: false,
        input_tokens: 24, output_tokens: 8, latency_ms: 53,
      },
    ],
  });
  await page.goto(root + "/runs/" + candidateId);
  await expect(page.getByTestId("judge-panel")).toContainText(t("judge.status.succeeded"));
  // Percentages and points follow Turkish number formatting.
  await expect(page.getByTestId("judge-panel")).toContainText("%87,5");
  await expect(page.getByTestId("judge-panel")).toContainText(t("format.points", { value: "+12,5" }));
  await expect(page.getByText("Strong grounded answer.", { exact: true })).toBeVisible();

  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await expectNoHorizontalOverflow(page);
    await settleReveals(page);
    await page.screenshot({ path: `/tmp/nexora-evaluations-${width}.png`, fullPage: true, animations: "disabled" });
  }
  // The sidebar is part of the desktop layout, so the rest of the flow needs it back.
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole("link", { name: t("evaluations.viewBaseline") }).click();
  await expect(page.getByRole("heading", { name: "Baseline v1" })).toBeVisible();
  await page.goto(root + "/suites/" + suiteId + "?cursor=invalid");
  await expect(page.getByRole("heading", { name: t("common.invalidLinkTitle") })).toBeVisible();
  provider.historyUnavailable(true);
  await page.goto(root + "/suites/" + suiteId);
  await expect(page.getByRole("heading", { name: "Support quality" })).toBeVisible();
  await expect(page.getByText(t("evaluations.unavailableBody"))).toBeVisible();
  provider.historyUnavailable(false);
  workspace.role = "member";
  await page.goto(root + "/suites/" + suiteId);
  await expect(page.getByText(t("evaluations.historyMemberNotice"))).toBeVisible();
  await expect(page.getByRole("link", { name: "Candidate v2", exact: true })).toHaveCount(0);
  await page.goto(root + "/runs/" + candidateId);
  await expect(page.getByRole("heading", { name: t("evaluations.deniedTitle") })).toBeVisible();
  await expect(page.locator(".codeblock")).toHaveCount(0);
  workspace.role = "owner";
  await page.goto(detailUrl);
}
