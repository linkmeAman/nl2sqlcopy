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
    raw = await asyncpg.connect(settings.database_url, timeout=10)
    try:
        await raw.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await raw.close()

    _pool = await asyncpg.create_pool(
        settings.database_url,
        init=_init_conn,
        max_size=10,
        max_inactive_connection_lifetime=300,
        command_timeout=30,
        timeout=10,
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

CREATE TABLE IF NOT EXISTS nl2sql_learned_patterns (
    id              SERIAL PRIMARY KEY,
    query_text      TEXT NOT NULL,
    sql_used        TEXT NOT NULL,
    tables_used     TEXT[] NOT NULL,
    join_conditions JSONB NOT NULL DEFAULT '[]',
    matched_groups  TEXT[] NOT NULL DEFAULT '{{}}',
    use_count       INTEGER NOT NULL DEFAULT 1,
    last_used_at    TIMESTAMP DEFAULT NOW(),
    created_at      TIMESTAMP DEFAULT NOW(),
    is_active       BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS nl2sql_user_instructions (
    id                SERIAL PRIMARY KEY,
    instruction_type  VARCHAR(50) NOT NULL,
    content           TEXT NOT NULL,
    embedding_source  TEXT NOT NULL,
    tables_affected   TEXT[] NOT NULL DEFAULT '{{}}',
    confidence_score  FLOAT NOT NULL DEFAULT 0.7,
    is_verified       BOOLEAN NOT NULL DEFAULT FALSE,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    conflict_group    INTEGER REFERENCES nl2sql_user_instructions(id)
                      ON DELETE SET NULL,
    source_query      TEXT,
    use_count         INTEGER NOT NULL DEFAULT 0,
    success_count     INTEGER NOT NULL DEFAULT 0,
    failure_count     INTEGER NOT NULL DEFAULT 0,
    last_used_at      TIMESTAMP,
    created_at        TIMESTAMP DEFAULT NOW(),
    updated_at        TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS nl2sql_request_events (
    id                   BIGSERIAL PRIMARY KEY,
    request_id           TEXT NOT NULL,
    endpoint             TEXT NOT NULL,
    query_text           TEXT,
    top_k                INTEGER,
    status               TEXT NOT NULL,
    attempt_count        INTEGER,
    latency_ms           INTEGER NOT NULL,
    stage_latencies_ms   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    warning_codes        JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_source         TEXT,
    metadata             JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS nl2sql_request_events_request_id_idx
    ON nl2sql_request_events (request_id);

CREATE INDEX IF NOT EXISTS nl2sql_request_events_endpoint_created_idx
    ON nl2sql_request_events (endpoint, created_at DESC);

CREATE TABLE IF NOT EXISTS nl2sql_benchmark_cases (
    id                BIGSERIAL PRIMARY KEY,
    query_text        TEXT NOT NULL,
    gold_sql          TEXT,
    expected_status   TEXT NOT NULL DEFAULT 'ok',
    slices            JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_label       TEXT,
    source            TEXT NOT NULL DEFAULT 'manual',
    metadata          JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS nl2sql_benchmark_cases_created_idx
    ON nl2sql_benchmark_cases (created_at DESC);

CREATE INDEX IF NOT EXISTS nl2sql_benchmark_cases_active_idx
    ON nl2sql_benchmark_cases (is_active, created_at DESC);
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


async def insert_request_event(
    pool: asyncpg.Pool,
    *,
    request_id: str,
    endpoint: str,
    query_text: str,
    top_k: int | None,
    status: str,
    latency_ms: int,
    attempt_count: int | None = None,
    stage_latencies_ms: dict[str, int] | None = None,
    warning_codes: list[str] | None = None,
    error_source: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Persist request telemetry for evaluation and replay pipelines."""
    sql = """
        INSERT INTO nl2sql_request_events (
            request_id,
            endpoint,
            query_text,
            top_k,
            status,
            attempt_count,
            latency_ms,
            stage_latencies_ms,
            warning_codes,
            error_source,
            metadata
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10, $11::jsonb)
    """
    async with pool.acquire() as conn:
        await conn.execute(
            sql,
            request_id,
            endpoint,
            query_text,
            top_k,
            status,
            attempt_count,
            latency_ms,
            json.dumps(stage_latencies_ms or {}),
            json.dumps(warning_codes or []),
            error_source,
            json.dumps(metadata or {}),
        )


async def list_recent_request_events(
    pool: asyncpg.Pool,
    *,
    limit: int = 50,
    endpoint: str | None = None,
) -> list[dict]:
    """Return recent request telemetry rows for operational inspection."""
    safe_limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        if endpoint:
            rows = await conn.fetch(
                """
                SELECT
                    request_id,
                    endpoint,
                    query_text,
                    top_k,
                    status,
                    attempt_count,
                    latency_ms,
                    stage_latencies_ms,
                    warning_codes,
                    error_source,
                    metadata,
                    created_at
                FROM nl2sql_request_events
                WHERE endpoint = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                endpoint,
                safe_limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT
                    request_id,
                    endpoint,
                    query_text,
                    top_k,
                    status,
                    attempt_count,
                    latency_ms,
                    stage_latencies_ms,
                    warning_codes,
                    error_source,
                    metadata,
                    created_at
                FROM nl2sql_request_events
                ORDER BY created_at DESC
                LIMIT $1
                """,
                safe_limit,
            )

    return [dict(row) for row in rows]


async def get_telemetry_summary(
    pool: asyncpg.Pool,
    *,
    endpoint: str | None = None,
    since_minutes: int | None = None,
) -> dict:
    """Return aggregate telemetry metrics for recent request events."""
    where_parts: list[str] = []
    params: list[object] = []
    if endpoint:
        params.append(endpoint)
        where_parts.append(f"endpoint = ${len(params)}")
    if since_minutes is not None:
        safe_since = max(1, since_minutes)
        params.append(safe_since)
        where_parts.append(f"created_at >= NOW() - (${len(params)}::int * INTERVAL '1 minute')")

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    query = f"""
        WITH filtered AS (
            SELECT *
            FROM nl2sql_request_events
            {where_sql}
        )
        SELECT
            COUNT(*)::bigint AS total_requests,
            COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0)::bigint AS ok_count,
            COALESCE(SUM(CASE WHEN status = 'clarification_needed' THEN 1 ELSE 0 END), 0)::bigint AS clarification_count,
            COALESCE(SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END), 0)::bigint AS rejected_count,
            COALESCE(SUM(CASE WHEN metadata->>'review_failed' = 'true' THEN 1 ELSE 0 END), 0)::bigint AS review_failed_count,
            COALESCE(AVG(latency_ms), 0)::double precision AS avg_latency_ms,
            COALESCE(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_ms), 0)::double precision AS p50_latency_ms,
            COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms), 0)::double precision AS p95_latency_ms
        FROM filtered
    """

    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *params)
        error_rows = await conn.fetch(
            f"""
            SELECT error_source, COUNT(*)::bigint AS count
            FROM nl2sql_request_events
            {where_sql}
            GROUP BY error_source
            ORDER BY count DESC
            """,
            *params,
        )

    total = int(row["total_requests"] or 0)
    return {
        "total_requests": total,
        "ok_count": int(row["ok_count"] or 0),
        "clarification_count": int(row["clarification_count"] or 0),
        "rejected_count": int(row["rejected_count"] or 0),
        "ok_rate": (float(row["ok_count"]) / total) if total else 0.0,
        "clarification_rate": (float(row["clarification_count"]) / total) if total else 0.0,
        "rejected_rate": (float(row["rejected_count"]) / total) if total else 0.0,
        "review_failed_count": int(row["review_failed_count"] or 0),
        "review_failed_rate": (float(row["review_failed_count"]) / total) if total else 0.0,
        "avg_latency_ms": int(round(float(row["avg_latency_ms"] or 0))),
        "p50_latency_ms": int(round(float(row["p50_latency_ms"] or 0))),
        "p95_latency_ms": int(round(float(row["p95_latency_ms"] or 0))),
        "error_sources": [
            {"error_source": r["error_source"], "count": int(r["count"] or 0)}
            for r in error_rows
            if r["error_source"] is not None
        ],
    }


async def insert_benchmark_case(
    pool: asyncpg.Pool,
    *,
    query_text: str,
    gold_sql: str | None,
    expected_status: str,
    slices: list[str] | None = None,
    error_label: str | None = None,
    source: str = "manual",
    metadata: dict | None = None,
) -> int:
    """Insert a benchmark case and return its id."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO nl2sql_benchmark_cases (
                query_text,
                gold_sql,
                expected_status,
                slices,
                error_label,
                source,
                metadata
            )
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb)
            RETURNING id
            """,
            query_text,
            gold_sql,
            expected_status,
            json.dumps(slices or []),
            error_label,
            source,
            json.dumps(metadata or {}),
        )
    return int(row["id"])


async def get_group_embedding_status(pool: asyncpg.Pool) -> list[dict]:
    """Return the stored schema_version and last_embedded_at per schema-group source."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                source,
                metadata->>'schema_version'   AS stored_version,
                MAX(created_at)               AS last_embedded_at
            FROM nl2sql_embeddings
            WHERE metadata->>'type' = 'schema_group'
            GROUP BY source, metadata->>'schema_version'
            ORDER BY source
            """
        )
    return [dict(row) for row in rows]


async def list_benchmark_cases(
    pool: asyncpg.Pool,
    *,
    limit: int = 100,
    active_only: bool = True,
) -> list[dict]:
    """Return benchmark cases ordered from newest to oldest."""
    safe_limit = max(1, min(limit, 1000))
    async with pool.acquire() as conn:
        if active_only:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    query_text,
                    gold_sql,
                    expected_status,
                    slices,
                    error_label,
                    source,
                    metadata,
                    is_active,
                    created_at
                FROM nl2sql_benchmark_cases
                WHERE is_active = TRUE
                ORDER BY created_at DESC
                LIMIT $1
                """,
                safe_limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    query_text,
                    gold_sql,
                    expected_status,
                    slices,
                    error_label,
                    source,
                    metadata,
                    is_active,
                    created_at
                FROM nl2sql_benchmark_cases
                ORDER BY created_at DESC
                LIMIT $1
                """,
                safe_limit,
            )

    return [dict(row) for row in rows]
