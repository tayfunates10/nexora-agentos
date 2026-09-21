import test from "node:test";
import assert from "node:assert/strict";
import {
  agentInput,
  agentSchema,
  eventDetail,
  runDurationSeconds,
  runEventKey,
  runEventPageSchema,
  runInput,
  runPageSchema,
  runResultSchema,
  runSchema,
  runSummarySchema,
} from "../lib/agent-contracts.ts";
import { formatDuration, formatTimestamp } from "../lib/i18n/format.ts";
import { createUi } from "../lib/i18n/messages.ts";

const en = createUi("en");
const tr = createUi("tr");
const duration = (finished: string, ui = en) =>
  formatDuration(runDurationSeconds({ created_at: run.created_at, finished_at: finished })!, ui.t);

const id = "6f1c9a2e-1b3d-4f5a-8c7e-9d0b1a2c3d4e";
const other = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const agent = {
  id, workspace_id: other, name: "Research agent",
  instructions: "Answer with citations.", model_profile: "default",
  created_at: "2026-09-20T09:00:00Z",
};
const run = {
  id, workspace_id: other, agent_id: other, trace_id: id,
  status: "queued", attempt_count: 0, cancel_requested_at: null, finished_at: null,
  failure_code: null, created_at: "2026-09-20T09:00:00Z", updated_at: "2026-09-20T09:00:00Z",
};
const summary = { ...run, agent_name: "Research agent", requested_by_me: true };
const result = {
  run_id: id, workspace_id: other, agent_id: other, trace_id: id, status: "succeeded",
  output_text: "The answer.", finish_reason: "stop", failure_code: null,
  recorded_input_tokens: 120, recorded_output_tokens: 40,
  selected_tools: ["search"],
  model_steps: [{
    step_no: 0, provider: "openai", model: "gpt-test", finish_reason: "stop",
    input_tokens: 120, output_tokens: 40,
  }],
};

test("agent contract accepts the API shape and rejects an unusable profile", () => {
  assert.equal(agentSchema.parse(agent).model_profile, "default");
  assert.equal(agentSchema.safeParse({ ...agent, model_profile: "" }).success, false);
  assert.equal(agentInput.safeParse({
    name: " Support ", instructions: " Be brief. ", model_profile: "balanced-v1",
  }).success, true);
  // The profile is operator configuration; free text would fail at the API anyway.
  assert.equal(agentInput.safeParse({
    name: "Support", instructions: "Be brief.", model_profile: "balanced v1",
  }).success, false);
  assert.equal(agentInput.safeParse({
    name: "", instructions: "Be brief.", model_profile: "default",
  }).success, false);
  assert.equal(agentInput.safeParse({
    name: "Support", instructions: "x".repeat(20_001), model_profile: "default",
  }).success, false);
});

test("run input requires an agent identifier and non-empty work", () => {
  assert.equal(runInput.safeParse({ agent_id: id, input: " Summarise " }).success, true);
  assert.equal(runInput.safeParse({ agent_id: "not-a-uuid", input: "Summarise" }).success, false);
  assert.equal(runInput.safeParse({ agent_id: id, input: "   " }).success, false);
});

test("run contract ties a terminal status to a finish time", () => {
  assert.equal(runSchema.parse(run).status, "queued");
  assert.equal(runSummarySchema.parse(summary).agent_name, "Research agent");
  // A finished run without a finish time, or a queued run with one, is not a state the API produces.
  assert.equal(runSchema.safeParse({ ...run, status: "succeeded" }).success, false);
  assert.equal(runSchema.safeParse({
    ...run, finished_at: "2026-09-20T09:05:00Z",
  }).success, false);
  assert.equal(runSchema.safeParse({
    ...run, status: "cancelled", finished_at: "2026-09-20T09:05:00Z",
  }).success, true);
  assert.equal(runSchema.safeParse({ ...run, status: "unknown" }).success, false);
});

test("run page and event page contracts bound their cursors", () => {
  assert.equal(runPageSchema.parse({ items: [summary], next_cursor: null }).items.length, 1);
  assert.equal(runPageSchema.safeParse({ items: [summary], next_cursor: "later" }).success, false);
  const events = {
    items: [{
      id, event_no: 1, event_type: "run.queued", payload: { request_id: "abc" },
      created_at: "2026-09-20T09:00:00Z",
    }],
    next_cursor: null,
  };
  assert.equal(runEventPageSchema.parse(events).items[0].event_no, 1);
  assert.equal(runEventPageSchema.safeParse({
    ...events, items: [{ ...events.items[0], event_no: 0 }],
  }).success, false);
});

test("result contract refuses an answer attached to a non-successful run", () => {
  assert.equal(runResultSchema.parse(result).output_text, "The answer.");
  assert.equal(runResultSchema.safeParse({
    ...result, status: "failed", failure_code: "model_error",
  }).success, false);
  assert.equal(runResultSchema.parse({
    ...result, status: "failed", output_text: null, finish_reason: null,
    failure_code: "model_error",
  }).status, "failed");
  assert.equal(runResultSchema.safeParse({ ...result, finish_reason: "other" }).success, false);
});

test("timeline formatting keeps worker payloads readable and inert", () => {
  assert.equal(en.t(runEventKey("run.queued")!), "Queued");
  assert.equal(tr.t(runEventKey("run.queued")!), "Kuyruğa alındı");
  // An event type the console does not know has no label, so the page falls back to the
  // recorded identifier rather than dropping the event.
  assert.equal(runEventKey("worker.new_event"), null);
  assert.equal(eventDetail({ request_id: "abc" }), "");
  assert.equal(eventDetail({ attempt: 2, failure_code: null }), "attempt: 2 · failure_code: —");
  assert.equal(eventDetail({ nested: { a: 1 } }), 'nested: {"a":1}');
  assert.equal(eventDetail({ long: "x".repeat(200) }).length, "long: ".length + 118);
  assert.equal(eventDetail({ script: "<script>alert(1)</script>" }),
    "script: <script>alert(1)</script>");
});

test("durations and timestamps are reported in UTC in both languages", () => {
  assert.equal(runDurationSeconds(run), null);
  assert.equal(duration("2026-09-20T09:00:42Z"), "42 sec");
  assert.equal(duration("2026-09-20T09:03:05Z"), "3 min 5 sec");
  assert.equal(duration("2026-09-20T11:30:00Z"), "2 hr 30 min");
  assert.equal(duration("2026-09-20T09:03:05Z", tr), "3 dk 5 sn");
  // A clock skew that puts the finish before the start never renders as a negative duration.
  assert.equal(runDurationSeconds({
    created_at: run.created_at, finished_at: "2026-09-20T08:59:00Z",
  }), 0);
  // The written form follows the language; the instant and its zone never do.
  assert.equal(formatTimestamp("2026-09-20T09:00:00Z", "en"), "20 Sept 2026, 09:00 UTC");
  assert.match(formatTimestamp("2026-09-20T09:00:00Z", "tr"), /^20 Eyl 2026,? 09:00 UTC$/);
});
