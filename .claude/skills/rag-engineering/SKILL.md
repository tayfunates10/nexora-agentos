---
name: rag-engineering
description: Use when implementing document ingestion, chunking, embeddings, pgvector, hybrid retrieval, reranking, citations, context assembly, or permission-aware RAG.
---

# RAG Engineering

Optimize retrieval quality before prompt complexity.

## Pipeline
Ingest -> parse -> normalize -> chunk -> enrich metadata -> embed -> index -> retrieve -> permission filter -> rerank -> context pack -> generate -> cite -> evaluate.

## Requirements
- Keep source_id, version, workspace_id and ACL metadata with every chunk.
- Permission filtering is mandatory before context reaches the model.
- Support deterministic re-indexing and deletion.
- Preserve provenance sufficient for user-visible citations.
- Measure retrieval separately from answer generation.

## Evaluation
Track recall@k / hit rate on retrieval test sets plus answer groundedness and citation correctness.
Do not claim quality based only on subjective demo prompts.
