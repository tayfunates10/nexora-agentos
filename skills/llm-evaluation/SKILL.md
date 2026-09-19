---
name: llm-evaluation
description: Use when adding AI eval datasets, regression tests, groundedness checks, tool-use evaluation, judge models, quality gates, or model comparisons.
---

# LLM Evaluation

Treat probabilistic behavior as measurable software behavior.

## Eval types
- Retrieval relevance.
- Tool selection and argument correctness.
- Task completion.
- Groundedness/citation fidelity.
- Safety/policy compliance.
- Latency and cost.

## Rules
- Keep a versioned golden dataset.
- Separate deterministic assertions from model-judge scores.
- Pin judge prompts/models for comparable regression runs.
- Store raw outputs for failed cases.
- Compare candidate vs baseline; do not rely only on absolute scores.

## CI
Fast smoke evals may gate PRs. Larger suites run asynchronously before release or on schedule.
