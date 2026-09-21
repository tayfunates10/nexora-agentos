import test from "node:test";
import assert from "node:assert/strict";
import {
  approvalPageSchema,
  approvalSchema,
  decisionInput,
  expiresIn,
  formatArguments,
  forcesApproval,
  policyInput,
  policyLabel,
  policyTone,
  toolPageSchema,
  toolSchema,
} from "../lib/tool-contracts.ts";

const id = "2f3a5c7e-9b1d-4e6f-8a2c-3d4e5f6a7b8c";
const other = "9a8b7c6d-5e4f-4a3b-8c1d-0e9f8a7b6c5d";
const tool = {
  id, workspace_id: other, name: "delete-record", server_key: "ops", remote_name: "delete",
  description: "Delete an archived record.",
  input_schema: { type: "object", properties: { record_id: { type: "string" } } },
  output_schema: null, side_effect: "destructive", enabled: true,
  policy_decision: "allow", policy_reason: "Allowed for the operations team.",
  policy_updated_at: "2026-09-20T10:00:00Z",
  created_at: "2026-09-20T09:00:00Z", updated_at: "2026-09-20T09:00:00Z",
};
const approval = {
  id, workspace_id: other, run_id: other, tool_call_id: id,
  requested_action: "delete-record",
  normalized_arguments: { record_id: "R-4471" },
  requester_subject: "alice", approver_subject: null, status: "pending",
  policy_reason: "Destructive tools always require a human decision.",
  expires_at: "2026-09-20T16:05:00Z", created_at: "2026-09-20T15:50:00Z", decided_at: null,
};

test("tool contract keeps a policy whole or absent", () => {
  assert.equal(toolSchema.parse(tool).policy_decision, "allow");
  const unpoliced = { ...tool, policy_decision: null, policy_reason: null, policy_updated_at: null };
  assert.equal(toolSchema.parse(unpoliced).policy_decision, null);
  // A decision without its reason, or a reason without a decision, is not a stored policy.
  assert.equal(toolSchema.safeParse({ ...tool, policy_reason: null }).success, false);
  assert.equal(toolSchema.safeParse({ ...unpoliced, policy_reason: "orphan" }).success, false);
  assert.equal(toolSchema.safeParse({ ...tool, side_effect: "anything" }).success, false);
  assert.equal(toolPageSchema.parse({ items: [tool], next_cursor: "delete-record" }).items.length, 1);
});

test("an unpoliced tool reads as denied, never as allowed", () => {
  const unpoliced = { ...tool, policy_decision: null, policy_reason: null, policy_updated_at: null };
  const parsed = toolSchema.parse(unpoliced);
  assert.equal(policyLabel(parsed), "No policy · denied by default");
  assert.equal(policyTone(parsed), "down");
  assert.equal(policyLabel(toolSchema.parse(tool)), "Allow");
  assert.equal(policyTone(toolSchema.parse(tool)), "up");
});

test("allow never removes the human step from a destructive or external tool", () => {
  assert.equal(forcesApproval(toolSchema.parse(tool)), true);
  assert.equal(forcesApproval(toolSchema.parse({
    ...tool, side_effect: "external_communication",
  })), true);
  assert.equal(forcesApproval(toolSchema.parse({ ...tool, side_effect: "write" })), false);
  assert.equal(forcesApproval(toolSchema.parse({
    ...tool, side_effect: "destructive", policy_decision: "require_approval",
  })), false);
});

test("approval contract ties a closed status to its decision time", () => {
  assert.equal(approvalSchema.parse(approval).status, "pending");
  assert.equal(approvalSchema.safeParse({ ...approval, status: "approved" }).success, false);
  assert.equal(approvalSchema.parse({
    ...approval, status: "rejected", decided_at: "2026-09-20T16:00:00Z",
    approver_subject: "admin",
  }).approver_subject, "admin");
  assert.equal(approvalSchema.safeParse({
    ...approval, decided_at: "2026-09-20T16:00:00Z",
  }).success, false);
  assert.equal(approvalPageSchema.parse({ items: [approval], next_cursor: null }).items.length, 1);
});

test("only an approve or reject decision is submittable", () => {
  assert.equal(decisionInput.parse({ decision: "approved" }).decision, "approved");
  assert.equal(decisionInput.safeParse({ decision: "expired" }).success, false);
  assert.equal(decisionInput.safeParse({ decision: "" }).success, false);
  assert.equal(policyInput.parse({ decision: "deny", reason: " No " }).reason, "No");
  assert.equal(policyInput.safeParse({ decision: "deny", reason: "" }).success, false);
  assert.equal(policyInput.safeParse({ decision: "maybe", reason: "why" }).success, false);
});

test("remaining approval time is reported without going negative", () => {
  const now = Date.parse("2026-09-20T15:50:00Z");
  assert.equal(expiresIn(approval, now), "15m left");
  assert.equal(expiresIn(approval, Date.parse("2026-09-20T16:04:30Z")), "30s left");
  assert.equal(expiresIn(approval, Date.parse("2026-09-20T16:06:00Z")), "expired");
  assert.equal(expiresIn({ expires_at: "2026-09-20T18:20:00Z" }, now), "2h left");
});

test("arguments are shown exactly as stored, as inert text", () => {
  assert.equal(
    formatArguments({ record_id: "R-4471", nested: { confirm: true } }),
    '{\n  "record_id": "R-4471",\n  "nested": {\n    "confirm": true\n  }\n}',
  );
  assert.equal(formatArguments({ note: "</pre><script>alert(1)</script>" }).includes("<script>"), true);
});
