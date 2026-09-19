---
name: cost-performance
description: Use when optimizing latency, token usage, model cost, caching, rate limits, concurrency, database performance, or capacity.
---

# Cost & Performance

Optimize using measurements.

## AI cost
Track tokens and provider cost per run, agent and workspace.
Use smaller/cheaper models only when evals prove quality is acceptable.
Cache deterministic or safely reusable results.

## Latency
Measure p50/p95/p99 and break latency down by retrieval, model, tools and queue wait.

## Capacity
Apply concurrency limits, rate limits and backpressure.
Protect expensive model/tool endpoints from unbounded fan-out.

## Rule
A cost optimization that degrades measured quality beyond the accepted threshold is not an optimization.
