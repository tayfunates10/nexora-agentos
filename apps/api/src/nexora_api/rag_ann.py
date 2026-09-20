"""Operator-managed pgvector HNSW indexes for variable-dimension RAG embeddings."""

from __future__ import annotations

import argparse
import hashlib

import psycopg
from psycopg import sql

from nexora_api.config import Settings

MAX_HNSW_VECTOR_DIMENSIONS = 2000
HNSW_MIN_M = 2
HNSW_MAX_M = 100
HNSW_MIN_EF_CONSTRUCTION = 4
HNSW_MAX_EF_CONSTRUCTION = 1000
HNSW_MIN_EF_SEARCH = 1
HNSW_MAX_EF_SEARCH = 1000


def validate_ann_target(embedding_model: str, dimensions: int) -> tuple[str, int]:
    model = embedding_model.strip()
    if not 1 <= len(model) <= 128:
        raise ValueError("embedding_model must be between 1 and 128 characters")
    if not 1 <= dimensions <= MAX_HNSW_VECTOR_DIMENSIONS:
        raise ValueError(
            f"HNSW vector dimensions must be between 1 and {MAX_HNSW_VECTOR_DIMENSIONS}"
        )
    return model, dimensions


def hnsw_index_name(embedding_model: str, dimensions: int) -> str:
    model, dimensions = validate_ann_target(embedding_model, dimensions)
    digest = hashlib.sha256(model.encode("utf-8")).hexdigest()[:12]
    return f"rag_chunks_hnsw_{dimensions}_{digest}"


def ensure_hnsw_index(
    connection,
    *,
    embedding_model: str,
    dimensions: int,
    m: int = 16,
    ef_construction: int = 64,
) -> str:
    """Create one online partial HNSW index for a reviewed model/dimension pair."""

    model, dimensions = validate_ann_target(embedding_model, dimensions)
    if not HNSW_MIN_M <= m <= HNSW_MAX_M:
        raise ValueError(f"m must be between {HNSW_MIN_M} and {HNSW_MAX_M}")
    if not HNSW_MIN_EF_CONSTRUCTION <= ef_construction <= HNSW_MAX_EF_CONSTRUCTION:
        raise ValueError(
            "ef_construction must be between "
            f"{HNSW_MIN_EF_CONSTRUCTION} and {HNSW_MAX_EF_CONSTRUCTION}"
        )
    name = hnsw_index_name(model, dimensions)
    connection.execute(
        sql.SQL(
            """CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name}
               ON rag_chunks USING hnsw
               ((embedding::vector({dimensions})) vector_cosine_ops)
               WITH (m={m}, ef_construction={ef_construction})
               WHERE embedding_model={model}
                 AND embedding_dimensions={dimensions}"""
        ).format(
            index_name=sql.Identifier(name),
            dimensions=sql.Literal(dimensions),
            m=sql.Literal(m),
            ef_construction=sql.Literal(ef_construction),
            model=sql.Literal(model),
        )
    )
    return name


def drop_hnsw_index(
    connection,
    *,
    embedding_model: str,
    dimensions: int,
) -> str:
    name = hnsw_index_name(embedding_model, dimensions)
    connection.execute(
        sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(sql.Identifier(name))
    )
    return name


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Nexora RAG HNSW indexes")
    parser.add_argument("action", choices=("ensure", "drop"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--dimensions", required=True, type=int)
    parser.add_argument("--m", type=int, default=16)
    parser.add_argument("--ef-construction", type=int, default=64)
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = Settings()
    # CREATE/DROP INDEX CONCURRENTLY cannot run inside a transaction.
    with psycopg.connect(
        settings.database_url.get_secret_value(),
        connect_timeout=5,
        autocommit=True,
    ) as connection:
        if args.action == "ensure":
            name = ensure_hnsw_index(
                connection,
                embedding_model=args.model,
                dimensions=args.dimensions,
                m=args.m,
                ef_construction=args.ef_construction,
            )
        else:
            name = drop_hnsw_index(
                connection,
                embedding_model=args.model,
                dimensions=args.dimensions,
            )
    print(name)


if __name__ == "__main__":
    main()
