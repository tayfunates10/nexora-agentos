import test from "node:test";
import assert from "node:assert/strict";
import {
  ingestionSchema,
  knowledgeSourcePageSchema,
  knowledgeSourceSchema,
  knowledgeTimestamp,
  sourceInput,
} from "../lib/knowledge-contracts.ts";

const id = "3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f";
const other = "4d5e6f7a-8b9c-4d1e-9f2a-3b4c5d6e7f8a";
const source = {
  id, source_key: "handbook", version: "v1", title: "Operations handbook",
  access_scope: "workspace", content_hash: "a".repeat(64), chunk_count: 12,
  created_at: "2026-09-20T17:05:00Z", updated_at: "2026-09-20T17:05:00Z",
};
const job = {
  id, workspace_id: other, source_key: "handbook", version: "v1",
  title: "Operations handbook", access_scope: "workspace", status: "queued", attempt_count: 0,
  source_id: null, chunk_count: null, embedding_input_tokens: null, error_code: null,
  created_at: "2026-09-20T17:00:00Z", updated_at: "2026-09-20T17:00:00Z", finished_at: null,
};

test("source contract matches the indexed shape the API returns", () => {
  assert.equal(knowledgeSourceSchema.parse(source).chunk_count, 12);
  assert.equal(knowledgeSourceSchema.safeParse({ ...source, content_hash: "short" }).success, false);
  assert.equal(knowledgeSourceSchema.safeParse({ ...source, access_scope: "public" }).success, false);
  assert.equal(
    knowledgeSourcePageSchema.parse({ items: [source], next_cursor: null }).items.length, 1,
  );
});

test("ingestion contract ties indexed content to a succeeded job", () => {
  assert.equal(ingestionSchema.parse(job).status, "queued");
  assert.equal(ingestionSchema.parse({
    ...job, status: "succeeded", source_id: other, chunk_count: 12,
    embedding_input_tokens: 4200, finished_at: "2026-09-20T17:05:00Z",
  }).chunk_count, 12);
  // A queued job that already claims an indexed source, or a success without one, is not real.
  assert.equal(ingestionSchema.safeParse({ ...job, source_id: other }).success, false);
  assert.equal(ingestionSchema.safeParse({ ...job, status: "succeeded" }).success, false);
  // A failure always names why nothing was indexed.
  assert.equal(ingestionSchema.safeParse({ ...job, status: "failed" }).success, false);
  assert.equal(ingestionSchema.parse({
    ...job, status: "failed", error_code: "workspace_budget_exhausted",
    finished_at: "2026-09-20T17:01:00Z",
  }).error_code, "workspace_budget_exhausted");
});

test("source input enforces the same bounds as the knowledge API", () => {
  assert.equal(sourceInput.parse({
    source_key: " handbook ", version: " v1 ", title: " Handbook ",
    text: " Escalate to the on-call lead. ", access_scope: "workspace",
  }).source_key, "handbook");
  // A key ends up in citations such as rag:handbook@v1, so its character set is bounded.
  assert.equal(sourceInput.safeParse({
    source_key: "hand book", version: "v1", title: "Handbook", text: "Body",
    access_scope: "workspace",
  }).success, false);
  assert.equal(sourceInput.safeParse({
    source_key: "handbook", version: "v1", title: "Handbook", text: "   ",
    access_scope: "workspace",
  }).success, false);
  assert.equal(sourceInput.safeParse({
    source_key: "handbook", version: "v1", title: "Handbook", text: "x".repeat(500_001),
    access_scope: "workspace",
  }).success, false);
  assert.equal(sourceInput.safeParse({
    source_key: "handbook", version: "v1", title: "Handbook", text: "Body",
    access_scope: "everyone",
  }).success, false);
});

test("knowledge timestamps are reported in UTC", () => {
  assert.equal(knowledgeTimestamp("2026-09-20T17:05:00Z"), "20 Sept 2026, 17:05 UTC");
});
