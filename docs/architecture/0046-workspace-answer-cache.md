# ADR 0046: Tenant-scoped exact answer cache

Status: implemented.

## Context

Repeated customer questions can create identical model-provider calls and cost even when the
agent, instructions and answer context have not changed. A global response cache is not acceptable:
Nexora is multi-tenant, agent instructions can differ, model routing can change, and answers based
on retrieval or external tools can become stale.

## Decision

Nexora adds an operator-controlled exact-answer cache for safe, reusable model answers.

The cache is partitioned by `workspace_id` and `agent_id`. Its key also includes the workspace,
agent kind, model profile, routed provider/model, a SHA-256 digest of the current agent
instructions, and a normalized question. Question normalization uses Unicode NFKC, whitespace
folding and case folding; this deliberately does not attempt semantic/paraphrase matching.

A profile enables caching with `answer_cache_ttl_seconds`. Zero disables it. The supported TTL is
up to 30 days.

The executor only consults this cache for step zero when:

- the selected profile enables caching;
- retrieval is disabled for the worker;
- the run has no advertised tools;
- the prior answer finished with `stop`, has non-empty text, has no tool calls, and has no
  structured output.

This means browser, connector, task, MCP and RAG-backed runs always re-read their dynamic context.
The cache is an optimization, never an authorization source.

On a cache hit Nexora writes a normal immutable `agent_model_steps` journal row so the run-result
contract remains unchanged. That cached step records zero provider input/output tokens, no spend
ledger row is created, and the routing reason is `workspace_answer_cache`. The cache's hit count
and last-used timestamp are updated in the same transaction.

## Tenant isolation and invalidation

A lookup always requires the exact `workspace_id`, `agent_id`, cache key, provider and model.
The database primary key begins with `workspace_id`, so two customers cannot address the same row.

The answer is naturally invalidated when the agent instructions, model profile, provider, model,
agent identity or normalized question changes. TTL expiry handles unchanged configurations over
time. Dynamic external data is excluded rather than cached.

## Cost and observability

`nexora_answer_cache_events_total{outcome="hit|miss|stored"}` measures cache behavior without
high-cardinality tenant labels. Cache hits do not call the model provider and therefore do not
increment provider token/cost metrics or consume a workspace model budget.

## Failure and retry semantics

The cached response is copied into the current run journal under the existing execution fence.
A cache miss proceeds through the normal budget check and provider path. Persisting a newly
cacheable answer happens transactionally with the model step and spend record, preserving the
existing durable-run guarantees.

## Verification

Unit tests cover normalized repeat questions, zero-token cache hits, workspace separation and the
rule that tool/retrieval-capable runs bypass the cache. PostgreSQL/Redis integration coverage proves
that a second durable run reuses the cached answer, records zero provider tokens and increments the
tenant cache hit counter.

Skills: agent-architecture, postgres-data-modeling, cost-performance, testing-quality,
security-threat-modeling.
