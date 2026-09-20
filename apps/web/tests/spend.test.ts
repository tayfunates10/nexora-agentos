import test from "node:test";
import assert from "node:assert/strict";
import {
  formatUnits,
  microsToUnits,
  offeredThresholds,
  parseThresholds,
  spendPeriod,
  spendRecordPageSchema,
  spendSummarySchema,
  unitsToMicros,
  usedRatio,
} from "../lib/spend-contracts.ts";

const id = "a2bdca9e-e07d-4b4a-ae19-6cc4e06e2d4c";
const summary = {
  workspace_id: id,
  period_start: "2026-09-01T00:00:00Z",
  period_end: "2026-10-01T00:00:00Z",
  consumed_micros: 7_500_000,
  monthly_limit_micros: 10_000_000,
  enforcement: "enforce",
  remaining_micros: 2_500_000,
  exhausted: false,
  alert_thresholds: [50, 100],
  alerts: [
    {
      threshold_percent: 50, monthly_limit_micros: 10_000_000,
      consumed_micros: 5_000_000, enforcement: "enforce",
      created_at: "2026-09-12T09:00:00Z",
    },
  ],
  categories: [
    { category: "agent_run", call_count: 3, input_tokens: 300, output_tokens: 90, cost_micros: 5_000_000 },
    { category: "evaluation_judge", call_count: 1, input_tokens: 40, output_tokens: 20, cost_micros: 2_500_000 },
  ],
};
const record = {
  id, source_key: "agent-run:" + id + ":step:0", category: "agent_run",
  provider: "openai", model: "gpt-test", input_tokens: 100, output_tokens: 30,
  cost_micros: 2_500_000, occurred_at: "2026-09-20T13:00:00Z",
};

test("summary contract requires the period total to match its own breakdown", () => {
  assert.equal(spendSummarySchema.parse(summary).consumed_micros, 7_500_000);
  assert.equal(spendSummarySchema.safeParse({ ...summary, consumed_micros: 9_000_000 }).success, false);
  assert.equal(spendSummarySchema.safeParse({ ...summary, remaining_micros: 9_000_000 }).success, false);
});

test("summary contract rejects impossible budget states", () => {
  // A limit without enforcement, or enforcement without a limit, is not a state the API can produce.
  assert.equal(spendSummarySchema.safeParse({ ...summary, enforcement: null }).success, false);
  assert.equal(spendSummarySchema.safeParse({
    ...summary, monthly_limit_micros: null, remaining_micros: null,
  }).success, false);
  // Exhausted is only reachable under enforcement, never in monitor mode.
  assert.equal(spendSummarySchema.safeParse({
    ...summary, enforcement: "monitor", consumed_micros: 10_000_000, remaining_micros: 0,
    exhausted: true, categories: [{ ...summary.categories[0], cost_micros: 10_000_000 }],
  }).success, false);
  assert.equal(spendSummarySchema.safeParse({ ...summary, period_end: summary.period_start }).success, false);
  assert.equal(spendSummarySchema.safeParse({ ...summary, consumed_micros: -1 }).success, false);
});

test("unmetered workspaces parse with no limit and no remaining", () => {
  const open = spendSummarySchema.parse({
    ...summary, monthly_limit_micros: null, enforcement: null, remaining_micros: null,
  });
  assert.equal(usedRatio(open), null);
});

test("a zero limit reads as fully used rather than dividing by zero", () => {
  const blocked = spendSummarySchema.parse({
    ...summary, monthly_limit_micros: 0, remaining_micros: 0, exhausted: true,
  });
  assert.equal(usedRatio(blocked), 1);
});

test("embedding rows carry input tokens only and stay inside the contract", () => {
  const embedding = {
    ...record, category: "embedding", model: "embed-test",
    source_key: "knowledge-source:" + id, input_tokens: 240, output_tokens: 0,
  };
  const page = spendRecordPageSchema.parse({ items: [embedding], next_cursor: null });
  assert.equal(page.items[0].category, "embedding");
  const withEmbeddings = spendSummarySchema.parse({
    ...summary,
    consumed_micros: 8_000_000,
    remaining_micros: 2_000_000,
    categories: [
      ...summary.categories,
      {
        category: "embedding", call_count: 2, input_tokens: 240,
        output_tokens: 0, cost_micros: 500_000,
      },
    ],
  });
  assert.equal(withEmbeddings.categories.length, 3);
});

test("record page contract bounds cursors and category values", () => {
  assert.equal(spendRecordPageSchema.parse({ items: [record], next_cursor: null }).items[0].cost_micros, 2_500_000);
  assert.equal(spendRecordPageSchema.safeParse({ items: [record], next_cursor: "../other" }).success, false);
  assert.equal(spendRecordPageSchema.safeParse({
    items: [{ ...record, category: "fine-tuning" }], next_cursor: null,
  }).success, false);
  assert.equal(spendRecordPageSchema.safeParse({
    items: [{ ...record, source_key: "x".repeat(201) }], next_cursor: null,
  }).success, false);
});

test("alert contracts require sorted thresholds and a crossing that happened", () => {
  assert.equal(spendSummarySchema.parse(summary).alerts[0].threshold_percent, 50);
  // Unsorted or duplicated thresholds would alert twice on one crossing.
  for (const invalid of [[100, 50], [50, 50], [0], [101], [1, 2, 3, 4, 5, 6]]) {
    assert.equal(spendSummarySchema.safeParse({ ...summary, alert_thresholds: invalid }).success, false);
  }
  // An alert whose own numbers do not reach its threshold is not evidence.
  assert.equal(spendSummarySchema.safeParse({
    ...summary,
    alerts: [{ ...summary.alerts[0], consumed_micros: 4_999_999 }],
  }).success, false);
});

test("threshold form values are validated and normalized", () => {
  assert.deepEqual(parseThresholds(["90", "50", "50"]), [50, 90]);
  assert.deepEqual(parseThresholds([]), []);
  for (const invalid of [["0"], ["101"], ["-5"], ["50.5"], ["abc"], [""], ["1e2"]]) {
    assert.throws(() => parseThresholds(invalid));
  }
  assert.throws(() => parseThresholds(["10", "20", "30", "40", "50", "60"]));
});

test("a threshold set outside the console stays on the form", () => {
  assert.deepEqual(offeredThresholds([]), [50, 75, 90, 100]);
  assert.deepEqual(offeredThresholds([85]), [50, 75, 85, 90, 100]);
  assert.deepEqual(offeredThresholds([50, 100]), [50, 75, 90, 100]);
});

test("units convert to exact micros without floating point drift", () => {
  for (const [input, expected] of [
    ["0", 0], ["1", 1_000_000], ["0.000001", 1], ["12.5", 12_500_000],
    ["0,25", 250_000], ["1000000000", 1_000_000_000_000_000], ["0.123456", 123_456],
  ] as const) assert.equal(unitsToMicros(input), expected);
  assert.equal(unitsToMicros(" 2.50 "), 2_500_000);
});

test("invalid or oversized amounts are refused before reaching the API", () => {
  for (const invalid of ["", "-1", "1.2345678", "abc", "1e6", "1 000", "1000000001", "."]) {
    assert.throws(() => unitsToMicros(invalid));
  }
});

test("micros render back to units and survive a round trip", () => {
  assert.equal(microsToUnits(0), "0.00");
  assert.equal(microsToUnits(500_000), "0.50");
  assert.equal(microsToUnits(1), "0.000001");
  assert.equal(microsToUnits(12_345_678), "12.345678");
  assert.equal(formatUnits(1_234_567_000_000), "1,234,567.00");
  for (const micros of [0, 1, 999_999, 12_345_678, 1_000_000_000_000_000]) {
    assert.equal(unitsToMicros(microsToUnits(micros)), micros);
  }
});

test("the period label names the inclusive last day of the window", () => {
  // Month spelling follows the runtime ICU data; the inclusive end date is the contract.
  assert.match(spendPeriod(spendSummarySchema.parse(summary)), /^1 Sept? 2026 – 30 Sept? 2026 UTC$/);
});
