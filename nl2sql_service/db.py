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


def _parse_json_value(value: object) -> object:
    """Best-effort parse for legacy stringified JSON payloads."""
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return value

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _coerce_str_list(value: object) -> list[str]:
    parsed = _parse_json_value(value)
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    if parsed is None:
        return []
    if isinstance(parsed, str):
        text = parsed.strip()
        return [text] if text else []
    return [str(parsed).strip()] if str(parsed).strip() else []


def _coerce_dict(value: object) -> dict[str, object]:
    parsed = _parse_json_value(value)
    if isinstance(parsed, dict):
        return parsed
    return {}


def _normalize_failure_log_row(row: dict) -> dict:
    normalized = dict(row)
    normalized["warning_codes"] = _coerce_str_list(normalized.get("warning_codes"))
    normalized["tables_attempted"] = _coerce_str_list(normalized.get("tables_attempted"))
    normalized["suggest_teach"] = _coerce_dict(normalized.get("suggest_teach"))
    if "failure_details" in normalized:
        normalized["failure_details"] = _coerce_dict(normalized.get("failure_details"))
    return normalized


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
    raw = await asyncpg.connect(
        settings.database_url,
        timeout=settings.db_connect_timeout,
    )
    try:
        await raw.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await raw.close()

    _pool = await asyncpg.create_pool(
        settings.database_url,
        init=_init_conn,
        min_size=1,
        max_size=settings.db_pool_max_size,
        max_inactive_connection_lifetime=300,
        command_timeout=settings.db_pool_command_timeout,
        timeout=settings.db_connect_timeout,
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
    USING hnsw (embedding vector_cosine_ops)
    WITH (m=16, ef_construction=64);

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
    instruction_embedding vector({settings.embedding_dimension}),
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

ALTER TABLE nl2sql_user_instructions
    ADD COLUMN IF NOT EXISTS instruction_embedding vector({settings.embedding_dimension});

CREATE INDEX IF NOT EXISTS nl2sql_user_instructions_embedding_hnsw_idx
    ON nl2sql_user_instructions
    USING hnsw (instruction_embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS nl2sql_pending_teach_confirmations (
    token             TEXT PRIMARY KEY,
    instruction       JSONB NOT NULL,
    conflicting_id    INTEGER REFERENCES nl2sql_user_instructions(id)
                      ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at        TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS nl2sql_pending_teach_confirmations_expires_idx
    ON nl2sql_pending_teach_confirmations (expires_at);

CREATE INDEX IF NOT EXISTS nl2sql_pending_teach_confirmations_created_idx
    ON nl2sql_pending_teach_confirmations (created_at);

CREATE TABLE IF NOT EXISTS nl2sql_request_events (
    id                   BIGSERIAL PRIMARY KEY,
    request_id           TEXT NOT NULL,
    trace_id             TEXT,
    correlation_id       TEXT,
    session_id           TEXT,
    workflow_id          TEXT,
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

CREATE TABLE IF NOT EXISTS nl2sql_cache_state (
    cache_key         TEXT PRIMARY KEY,
    cache_epoch       BIGINT NOT NULL DEFAULT 1,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO nl2sql_cache_state (cache_key, cache_epoch)
VALUES ('query_logic', 1)
ON CONFLICT (cache_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS nl2sql_query_cache (
    id                BIGSERIAL PRIMARY KEY,
    endpoint          TEXT NOT NULL,
    normalized_query  TEXT NOT NULL,
    top_k             INTEGER NOT NULL,
    response_json     JSONB NOT NULL,
    query_embedding   vector({settings.embedding_dimension}),
    cache_epoch       BIGINT NOT NULL,
    hit_count         INTEGER NOT NULL DEFAULT 0,
    last_hit_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (endpoint, normalized_query, top_k, cache_epoch)
);

CREATE INDEX IF NOT EXISTS nl2sql_query_cache_lookup_idx
    ON nl2sql_query_cache (endpoint, cache_epoch, top_k, normalized_query);

CREATE INDEX IF NOT EXISTS nl2sql_query_cache_recent_idx
    ON nl2sql_query_cache (endpoint, cache_epoch, updated_at DESC);

CREATE INDEX IF NOT EXISTS nl2sql_query_cache_embedding_hnsw_idx
    ON nl2sql_query_cache
    USING hnsw (query_embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS nl2sql_failure_log (
    id                BIGSERIAL PRIMARY KEY,
    request_id        TEXT NOT NULL,
    trace_id          TEXT,
    correlation_id    TEXT,
    session_id        TEXT,
    workflow_id       TEXT,
    endpoint          TEXT NOT NULL,
    query_text        TEXT NOT NULL,
    warning_codes     JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_source      TEXT,
    failure_type      TEXT,
    root_cause        TEXT,
    sql_preview       TEXT,
    tables_attempted  TEXT[] NOT NULL DEFAULT '{{}}',
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    suggest_teach     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    failure_details   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS nl2sql_failure_log_created_idx
    ON nl2sql_failure_log (created_at DESC);

CREATE INDEX IF NOT EXISTS nl2sql_failure_log_endpoint_idx
    ON nl2sql_failure_log (endpoint, created_at DESC);

CREATE TABLE IF NOT EXISTS nl2sql_trace_events (
    id              BIGSERIAL PRIMARY KEY,
    request_id      TEXT NOT NULL,
    trace_id        TEXT,
    correlation_id  TEXT,
    session_id      TEXT,
    workflow_id     TEXT,
    seq             INTEGER NOT NULL,
    event           TEXT,
    layer           TEXT NOT NULL,
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL,
    message         TEXT NOT NULL,
    span_id         TEXT,
    parent_span_id  TEXT,
    duration_ms     INTEGER,
    provider        TEXT,
    model           TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    reasoning_summary TEXT,
    input_summary   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    output_summary  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    warning_codes   JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_source    TEXT,
    token_usage     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    errors          JSONB NOT NULL DEFAULT '[]'::jsonb,
    details         JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    metadata        JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    schema_version  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (request_id, seq)
);

CREATE INDEX IF NOT EXISTS nl2sql_trace_events_request_idx
    ON nl2sql_trace_events (request_id, seq);

CREATE INDEX IF NOT EXISTS nl2sql_trace_events_created_idx
    ON nl2sql_trace_events (created_at DESC);

ALTER TABLE nl2sql_request_events ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE nl2sql_request_events ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE nl2sql_request_events ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE nl2sql_request_events ADD COLUMN IF NOT EXISTS workflow_id TEXT;

ALTER TABLE nl2sql_failure_log ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE nl2sql_failure_log ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE nl2sql_failure_log ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE nl2sql_failure_log ADD COLUMN IF NOT EXISTS workflow_id TEXT;
ALTER TABLE nl2sql_failure_log ADD COLUMN IF NOT EXISTS failure_type TEXT;
ALTER TABLE nl2sql_failure_log ADD COLUMN IF NOT EXISTS root_cause TEXT;
ALTER TABLE nl2sql_failure_log ADD COLUMN IF NOT EXISTS failure_details JSONB NOT NULL DEFAULT '{{}}'::jsonb;

ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS workflow_id TEXT;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS event TEXT;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS span_id TEXT;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS parent_span_id TEXT;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS reasoning_summary TEXT;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS input_summary JSONB NOT NULL DEFAULT '{{}}'::jsonb;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS output_summary JSONB NOT NULL DEFAULT '{{}}'::jsonb;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS token_usage JSONB NOT NULL DEFAULT '{{}}'::jsonb;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS errors JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;
ALTER TABLE nl2sql_trace_events ADD COLUMN IF NOT EXISTS schema_version TEXT;
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
    trace_id: str | None = None,
    correlation_id: str | None = None,
    session_id: str | None = None,
    workflow_id: str | None = None,
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
            trace_id,
            correlation_id,
            session_id,
            workflow_id,
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
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13::jsonb, $14, $15::jsonb)
    """
    async with pool.acquire() as conn:
        await conn.execute(
            sql,
            request_id,
            trace_id,
            correlation_id,
            session_id,
            workflow_id,
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


async def insert_failure_log(
    pool: asyncpg.Pool,
    *,
    request_id: str,
    trace_id: str | None = None,
    correlation_id: str | None = None,
    session_id: str | None = None,
    workflow_id: str | None = None,
    endpoint: str,
    query_text: str,
    warning_codes: list[str],
    error_source: str | None,
    failure_type: str | None = None,
    root_cause: str | None = None,
    sql_preview: str | None,
    tables_attempted: list[str],
    latency_ms: int,
    suggest_teach: dict,
    failure_details: dict | None = None,
) -> None:
    """Persist a failed request into the dedicated failure log table."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO nl2sql_failure_log (
                    request_id,
                    trace_id,
                    correlation_id,
                    session_id,
                    workflow_id,
                    endpoint,
                    query_text,
                    warning_codes,
                    error_source,
                    failure_type,
                    root_cause,
                    sql_preview,
                    tables_attempted,
                    latency_ms,
                    suggest_teach,
                    failure_details
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $13::text[], $14, $15::jsonb, $16::jsonb)
                """,
                request_id,
                trace_id,
                correlation_id,
                session_id,
                workflow_id,
                endpoint,
                query_text,
                json.dumps(warning_codes),
                error_source,
                failure_type,
                root_cause,
                sql_preview or "",
                tables_attempted,
                latency_ms,
                json.dumps(suggest_teach),
                json.dumps(failure_details or {}),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write failure log entry: %s", exc)


async def insert_trace_event(
    pool: asyncpg.Pool,
    *,
    request_id: str,
    trace_id: str | None = None,
    correlation_id: str | None = None,
    session_id: str | None = None,
    workflow_id: str | None = None,
    seq: int,
    event: str | None = None,
    layer: str,
    stage: str,
    status: str,
    message: str,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    duration_ms: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    retry_count: int = 0,
    reasoning_summary: str | None = None,
    input_summary: dict | None = None,
    output_summary: dict | None = None,
    warning_codes: list[str] | None = None,
    error_source: str | None = None,
    token_usage: dict | None = None,
    errors: list[str] | None = None,
    details: dict | None = None,
    metadata: dict | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    schema_version: str | None = None,
) -> None:
    """Persist one sanitized per-stage trace event for request debugging."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nl2sql_trace_events (
                request_id,
                trace_id,
                correlation_id,
                session_id,
                workflow_id,
                seq,
                event,
                layer,
                stage,
                status,
                message,
                span_id,
                parent_span_id,
                duration_ms,
                provider,
                model,
                retry_count,
                reasoning_summary,
                input_summary,
                output_summary,
                warning_codes,
                error_source,
                token_usage,
                errors,
                details,
                metadata,
                started_at,
                ended_at,
                schema_version
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19::jsonb, $20::jsonb, $21::jsonb, $22, $23::jsonb, $24::jsonb, $25::jsonb, $26::jsonb, $27, $28, $29)
            ON CONFLICT (request_id, seq) DO NOTHING
            """,
            request_id,
            trace_id,
            correlation_id,
            session_id,
            workflow_id,
            seq,
            event,
            layer,
            stage,
            status,
            message,
            span_id,
            parent_span_id,
            duration_ms,
            provider,
            model,
            retry_count,
            reasoning_summary,
            json.dumps(input_summary or {}),
            json.dumps(output_summary or {}),
            json.dumps(warning_codes or []),
            error_source,
            json.dumps(token_usage or {}),
            json.dumps(errors or []),
            json.dumps(details or {}),
            json.dumps(metadata or {}),
            started_at,
            ended_at,
            schema_version,
        )


async def list_failure_logs(
    pool: asyncpg.Pool,
    *,
    limit: int = 100,
    endpoint: str | None = None,
) -> list[dict]:
    """Return recent failure log entries for operational review."""
    safe_limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        if endpoint:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    request_id,
                    endpoint,
                    query_text,
                    warning_codes,
                    error_source,
                    sql_preview,
                    tables_attempted,
                    latency_ms,
                    suggest_teach,
                    created_at
                FROM nl2sql_failure_log
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
                    id,
                    request_id,
                    endpoint,
                    query_text,
                    warning_codes,
                    error_source,
                    sql_preview,
                    tables_attempted,
                    latency_ms,
                    suggest_teach,
                    created_at
                FROM nl2sql_failure_log
                ORDER BY created_at DESC
                LIMIT $1
                """,
                safe_limit,
            )
    return [_normalize_failure_log_row(dict(row)) for row in rows]


async def list_trace_events(
    pool: asyncpg.Pool,
    *,
    request_id: str,
    limit: int = settings.db_trace_events_limit_default,
) -> list[dict]:
    """Return ordered trace events for a single request id."""
    safe_limit = max(1, min(limit, 1000))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                request_id,
                trace_id,
                correlation_id,
                session_id,
                workflow_id,
                seq,
                event,
                layer,
                stage,
                status,
                message,
                span_id,
                parent_span_id,
                duration_ms,
                provider,
                model,
                retry_count,
                reasoning_summary,
                input_summary,
                output_summary,
                warning_codes,
                error_source,
                token_usage,
                errors,
                details,
                metadata,
                started_at,
                ended_at,
                schema_version,
                created_at
            FROM nl2sql_trace_events
            WHERE request_id = $1
            ORDER BY seq ASC
            LIMIT $2
            """,
            request_id,
            safe_limit,
        )
    return [dict(row) for row in rows]


async def list_recent_request_events(
    pool: asyncpg.Pool,
    *,
    limit: int = settings.db_recent_request_events_limit_default,
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
                    trace_id,
                    correlation_id,
                    session_id,
                    workflow_id,
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
                    trace_id,
                    correlation_id,
                    session_id,
                    workflow_id,
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


async def get_pending_teach_confirmation_stats(pool: asyncpg.Pool) -> dict[str, object]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE expires_at > NOW())::bigint AS pending_active_count,
                COUNT(*) FILTER (WHERE expires_at <= NOW())::bigint AS pending_expired_count,
                MIN(created_at) FILTER (WHERE expires_at > NOW()) AS oldest_pending_created_at,
                MIN(expires_at) FILTER (WHERE expires_at > NOW()) AS next_pending_expiry_at
            FROM nl2sql_pending_teach_confirmations
            """
        )
    return {
        "pending_active_count": int(row["pending_active_count"] if row else 0),
        "pending_expired_count": int(row["pending_expired_count"] if row else 0),
        "oldest_pending_created_at": row["oldest_pending_created_at"] if row else None,
        "next_pending_expiry_at": row["next_pending_expiry_at"] if row else None,
    }


async def list_pending_teach_confirmations(
    pool: asyncpg.Pool,
    *,
    limit: int = 100,
    include_expired: bool = False,
) -> list[dict[str, object]]:
    safe_limit = max(1, min(limit, 500))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                token,
                instruction,
                conflicting_id,
                created_at,
                expires_at,
                expires_at <= NOW() AS is_expired
            FROM nl2sql_pending_teach_confirmations
            WHERE $2::boolean = TRUE OR expires_at > NOW()
            ORDER BY created_at DESC
            LIMIT $1
            """,
            safe_limit,
            include_expired,
        )

    results: list[dict[str, object]] = []
    for row in rows:
        instruction = row["instruction"]
        if instruction is None:
            instruction_data: dict[str, object] = {}
        else:
            instruction_data = dict(instruction)
        results.append(
            {
                "token": str(row["token"]),
                "instruction_type": str(instruction_data.get("instruction_type") or ""),
                "content": str(instruction_data.get("content") or ""),
                "tables_affected": list(instruction_data.get("tables_affected") or []),
                "source_query": instruction_data.get("source_query"),
                "conflicting_id": row["conflicting_id"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "is_expired": bool(row["is_expired"]),
            }
        )
    return results


async def cleanup_pending_teach_confirmations(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM nl2sql_pending_teach_confirmations
            WHERE expires_at <= NOW()
            """
        )
    try:
        return int(result.split()[-1])
    except (AttributeError, IndexError, ValueError):
        return 0


def normalize_query_text(query: str) -> str:
    return " ".join(query.strip().lower().split())


async def get_query_cache_epoch(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT cache_epoch
            FROM nl2sql_cache_state
            WHERE cache_key = 'query_logic'
            """
        )
    return int(row["cache_epoch"] if row else 1)


async def bump_query_cache_epoch(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO nl2sql_cache_state (cache_key, cache_epoch)
                VALUES ('query_logic', 1)
                ON CONFLICT (cache_key) DO NOTHING
                """
            )
            row = await conn.fetchrow(
                """
                UPDATE nl2sql_cache_state
                SET cache_epoch = cache_epoch + 1,
                    updated_at = NOW()
                WHERE cache_key = 'query_logic'
                RETURNING cache_epoch
                """
            )
    return int(row["cache_epoch"] if row else 1)


async def upsert_query_cache_entry(
    pool: asyncpg.Pool,
    *,
    endpoint: str,
    query_text: str,
    top_k: int,
    response_json: dict,
    query_embedding: list[float] | None,
    cache_epoch: int,
) -> None:
    normalized_query = normalize_query_text(query_text)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO nl2sql_query_cache (
                endpoint,
                normalized_query,
                top_k,
                response_json,
                query_embedding,
                cache_epoch,
                hit_count,
                last_hit_at,
                updated_at
            )
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, 0, NOW(), NOW())
            ON CONFLICT (endpoint, normalized_query, top_k, cache_epoch)
            DO UPDATE SET
                response_json = EXCLUDED.response_json,
                query_embedding = EXCLUDED.query_embedding,
                updated_at = NOW()
            """,
            endpoint,
            normalized_query,
            top_k,
            json.dumps(response_json),
            query_embedding,
            cache_epoch,
        )


async def get_query_cache_exact(
    pool: asyncpg.Pool,
    *,
    endpoint: str,
    query_text: str,
    top_k: int,
    cache_epoch: int,
) -> dict | None:
    normalized_query = normalize_query_text(query_text)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, response_json
            FROM nl2sql_query_cache
            WHERE endpoint = $1
              AND normalized_query = $2
              AND top_k = $3
              AND cache_epoch = $4
            """,
            endpoint,
            normalized_query,
            top_k,
            cache_epoch,
        )
        if row is None:
            return None
        await conn.execute(
            """
            UPDATE nl2sql_query_cache
            SET hit_count = hit_count + 1,
                last_hit_at = NOW()
            WHERE id = $1
            """,
            row["id"],
        )
    return dict(row["response_json"] or {})


async def get_query_cache_semantic(
    pool: asyncpg.Pool,
    *,
    endpoint: str,
    query_embedding: list[float],
    top_k: int,
    cache_epoch: int,
    min_similarity: float,
) -> dict | None:
    max_distance = max(0.0, 1.0 - min_similarity)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                id,
                response_json,
                1 - (query_embedding <=> $5) AS similarity
            FROM nl2sql_query_cache
            WHERE endpoint = $1
              AND top_k = $2
              AND cache_epoch = $3
              AND query_embedding IS NOT NULL
              AND (query_embedding <=> $5) <= $4
            ORDER BY query_embedding <=> $5 ASC, updated_at DESC
            LIMIT 1
            """,
            endpoint,
            top_k,
            cache_epoch,
            max_distance,
            query_embedding,
        )
        if row is None:
            return None
        await conn.execute(
            """
            UPDATE nl2sql_query_cache
            SET hit_count = hit_count + 1,
                last_hit_at = NOW()
            WHERE id = $1
            """,
            row["id"],
        )
    return dict(row["response_json"] or {})


async def get_query_cache_stats(pool: asyncpg.Pool) -> dict[str, int]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::bigint AS db_query_cache_size,
                COALESCE(MAX(cache_epoch), 1)::bigint AS cache_epoch
            FROM nl2sql_query_cache
            """
        )
    return {
        "db_query_cache_size": int(row["db_query_cache_size"] if row else 0),
        "cache_epoch": int(row["cache_epoch"] if row else 1),
    }


async def clear_query_cache(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM nl2sql_query_cache")
    try:
        return int(result.split()[-1])
    except (AttributeError, IndexError, ValueError):
        return 0
