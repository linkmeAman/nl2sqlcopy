from __future__ import annotations

import json
import logging

import asyncpg
import pgvector.asyncpg  # registers the vector codec

from nl2sql_service.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

# ---------------------------------------------------------------------------
# Fixed columns in nl2sql_embeddings that are stored directly.
# Any extra keys in a chunk dict go into the ``metadata`` JSONB column.
# ---------------------------------------------------------------------------
_FIXED_COLUMNS = {"text", "source", "chunk_index", "token_count", "embedding_model"}


# ---------------------------------------------------------------------------
# Pool lifecycle
# ---------------------------------------------------------------------------


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Register the pgvector codec for every new connection in the pool."""
    await pgvector.asyncpg.register_vector(conn)


async def create_pool() -> asyncpg.Pool:
    global _pool
    # Install the extension on a plain connection first so the `vector` type
    # exists before the pool's _init_conn callback tries to look up its OID.
    raw = await asyncpg.connect(settings.database_url)
    try:
        await raw.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await raw.close()

    _pool = await asyncpg.create_pool(
        settings.database_url,
        init=_init_conn,
    )
    logger.info("asyncpg pool created")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("asyncpg pool closed")


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

_DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS nl2sql_embeddings (
    id              BIGSERIAL PRIMARY KEY,
    content         TEXT                        NOT NULL,
    embedding       vector({settings.embedding_dimension}) NOT NULL,
    source          TEXT                        NOT NULL,
    chunk_index     INT                         NOT NULL,
    token_count     INT                         NOT NULL,
    embedding_model TEXT                        NOT NULL,
    metadata        JSONB                       NOT NULL DEFAULT '{{}}',
    created_at      TIMESTAMPTZ                 NOT NULL DEFAULT NOW(),
    UNIQUE (source, chunk_index)
);

CREATE INDEX IF NOT EXISTS nl2sql_embed_hnsw_idx
    ON nl2sql_embeddings
    USING hnsw (embedding vector_cosine_ops);
"""


async def bootstrap(pool: asyncpg.Pool) -> None:
    """Idempotently create the extension, table, and HNSW index."""
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
    logger.info(
        "Bootstrap complete: nl2sql_embeddings table and HNSW index are ready "
        "(dim=%d)",
        settings.embedding_dimension,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _build_record(
    chunk: dict,
    embedding: list[float],
) -> tuple:
    """
    Build the positional tuple for the INSERT statement.

    Extra chunk keys beyond ``_FIXED_COLUMNS`` are serialised into ``metadata``.
    """
    metadata = {k: v for k, v in chunk.items() if k not in _FIXED_COLUMNS and k != "text"}
    return (
        chunk["text"],           # content
        embedding,               # embedding (registered as pgvector.Vector)
        chunk["source"],
        chunk["chunk_index"],
        chunk["token_count"],
        chunk["embedding_model"],
        json.dumps(metadata),    # metadata JSONB
    )


async def insert_chunks(
    pool: asyncpg.Pool,
    chunks: list[dict],
    embeddings: list[list[float]],
) -> int:
    """
    Insert chunks and their embeddings in a single transaction.

    Uses ON CONFLICT (source, chunk_index) DO NOTHING for idempotency.
    Returns the number of rows actually inserted.
    """
    if not chunks:
        return 0

    records = [_build_record(c, e) for c, e in zip(chunks, embeddings)]

    sql = """
        INSERT INTO nl2sql_embeddings
            (content, embedding, source, chunk_index, token_count, embedding_model, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (source, chunk_index) DO NOTHING
    """

    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.executemany(sql, records)

    # executemany returns a status string like "INSERT 0 N"; parse N
    try:
        inserted = int(result.split()[-1])
    except (AttributeError, ValueError, IndexError):
        inserted = len(records)

    logger.info("Inserted %d / %d chunks (source=%s)", inserted, len(records), chunks[0]["source"])
    return inserted
