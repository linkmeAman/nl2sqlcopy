from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Union

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from nl2sql_service import (
    answer_generator,
    cache,
    chunker,
    db,
    embed,
    help_docs,
    ingest,
    mysql_executor,
    pattern_store,
    query_rewriter,
    retrieve,
    schema_loader,
)
from nl2sql_service.cache import ask_cache
from nl2sql_service.config import settings
from nl2sql_service.embed import (
    EmbeddingClientError,
    EmbeddingDimensionError,
    EmbeddingTimeoutError,
    EmbeddingUpstreamError,
)
from nl2sql_service.instruction_store import (
    process_confirmation,
    process_teach_request,
)
from nl2sql_service.models import (
    AskRejected,
    AskRequest,
    AskResponse,
    AskSuccess,
    BenchmarkCaseCreateRequest,
    BenchmarkCaseCreateResponse,
    CacheSource,
    ConfirmRequest,
    EmbeddedIngestResponse,
    GenerateSqlRequest,
    GenerateSqlResponse,
    GroupEmbeddingStatusResponse,
    GroupQueryResponse,
    InstructionType,
    IngestGroupsResponse,
    IngestGroupsRequest,
    IngestKnowledgeRequest,
    IngestRequest,
    IngestResponse,
    IngestSchemaRequest,
    IngestTextRequest,
    LearningStatus,
    PatternFeedbackRequest,
    QueryRequest,
    QueryResponse,
    SqlWarning,
    TeachRequest,
    TeachResponse,
    WarningCode,
)
from nl2sql_service.rulebook import RULES, get_active_rules, get_config
from nl2sql_service.sql_generator import generate_sql, review_sql
from nl2sql_service.log_config import configure_logging, set_request_id

configure_logging()
logger = logging.getLogger(__name__)


def _resolve_request_id(candidate: str | None) -> str:
    if candidate and candidate.strip():
        return candidate.strip()
    return uuid.uuid4().hex


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


def _derive_error_source(warnings: list[SqlWarning]) -> str | None:
    if not warnings:
        return None
    for warning in warnings:
        if warning.code == WarningCode.REQUEST_TIMEOUT:
            return "service_timeout"
        if warning.code in {WarningCode.OLLAMA_TIMEOUT, WarningCode.OLLAMA_UPSTREAM, WarningCode.OLLAMA_MALFORMED}:
            return "generation_transport"
        if warning.code in {
            WarningCode.SQL_EMPTY,
            WarningCode.SQL_MULTI_STATEMENT,
            WarningCode.SQL_DESTRUCTIVE,
            WarningCode.SQL_NOT_SELECT,
            WarningCode.MAX_RETRIES_EXCEEDED,
        }:
            return "sql_generation"
        if warning.code in {
            WarningCode.TABLE_OUT_OF_SCOPE,
            WarningCode.COLUMN_OUT_OF_SCOPE,
            WarningCode.MYSQL_EXPLAIN_ERROR,
            WarningCode.MYSQL_EXPLAIN_UNAVAILABLE,
        }:
            return "schema_or_validation"
        if warning.code == WarningCode.MYSQL_QUERY_ERROR:
            return "execution"
        if warning.code in {
            WarningCode.ANSWER_TIMEOUT,
            WarningCode.ANSWER_UPSTREAM,
            WarningCode.ANSWER_MALFORMED,
            WarningCode.ANSWER_HALLUCINATION,
        }:
            return "answer_generation"
        if warning.code == WarningCode.REVIEW_FAILED:
            return "review_gate"
    return "unknown"


def _ask_success_from_cache(payload: dict) -> AskSuccess:
    """Reconstruct an AskSuccess from a cached serialised payload."""
    SKIP = {"cache_hit", "ask_cache_hit", "semantic_cache_hit", "_top_k", "status"}
    warnings_raw = payload.get("warnings") or []
    warnings = [
        SqlWarning(code=WarningCode(w["code"]), message=w.get("message", ""))
        for w in warnings_raw
        if isinstance(w, dict) and "code" in w
    ]
    react_trace_raw = payload.get("react_trace")
    from nl2sql_service.models import ReactTrace
    react_trace = ReactTrace(**react_trace_raw) if isinstance(react_trace_raw, dict) else react_trace_raw
    return AskSuccess(
        answer=payload.get("answer", ""),
        sql=payload.get("sql"),
        warnings=warnings,
        row_count=payload.get("row_count", 0),
        columns=payload.get("columns") or [],
        tables_used=payload.get("tables_used") or [],
        matched_groups=payload.get("matched_groups") or [],
        attempt_count=payload.get("attempt_count", 0),
        cache_hit=bool(payload.get("cache_hit", False)),
        cache_source=CacheSource(str(payload.get("cache_source", CacheSource.NONE.value))),
        react_trace=react_trace,
    )


async def _load_query_embedding(query: str) -> list[float] | None:
    query_vector = cache.embed_cache.get(query)
    if query_vector is not None:
        return query_vector

    vectors = await embed.embed_texts([query])
    if not vectors:
        return None
    query_vector = vectors[0]
    cache.embed_cache.set(query, query_vector)
    return query_vector


async def _invalidate_query_caches(pool: asyncpg.Pool) -> int:
    cache.clear_memory_caches()
    return await db.bump_query_cache_epoch(pool)


def _teach_mutates_cache(response: TeachResponse) -> bool:
    return response.learning_status in {
        LearningStatus.SAVED_NEW,
        LearningStatus.SIMILAR_FOUND,
        LearningStatus.CONFIRMED,
        LearningStatus.UPDATED_EXISTING,
    }


async def _log_request_event(pool: asyncpg.Pool, **kwargs: object) -> None:
    try:
        warning_codes = [str(code) for code in kwargs.get("warning_codes", []) or []]
        metadata = dict(kwargs.get("metadata", {}) or {})
        metadata["review_failed"] = WarningCode.REVIEW_FAILED.value in warning_codes
        kwargs["warning_codes"] = warning_codes
        kwargs["metadata"] = metadata
        await db.insert_request_event(pool, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write request telemetry")


def _ensure_governance_enabled() -> None:
    if not settings.governance_enabled:
        raise HTTPException(status_code=503, detail="Governance disabled")


def _generation_metadata(result: GenerateSqlResponse) -> dict[str, object]:
    base: dict[str, object] = {
        "has_react_trace": getattr(result, "react_trace", None) is not None,
    }
    if result.status == "ok":
        base["tables_used"] = result.tables_used
        base["matched_groups"] = result.matched_groups
    elif result.status == "clarification_needed":
        base["failure_reason"] = result.failure_reason
        base["suggestion_count"] = len(result.suggestions)
    return base


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — DB connection is non-fatal so the service starts even when
    # PostgreSQL is temporarily unreachable over Tailscale.
    await embed.init_client()
    app.state.pool_reconnect_lock = asyncio.Lock()
    app.state.pool_last_reconnect_attempt = 0.0
    try:
        pool = await db.create_pool()
        await db.bootstrap(pool)
        await ingest.ensure_hnsw_index(pool)
        app.state.pool = pool
        logger.info(
            "Service ready (embedding_dim=%d, top_k=%d)",
            settings.embedding_dimension,
            settings.top_k,
        )
    except Exception as exc:  # noqa: BLE001
        app.state.pool = None
        logger.error(
            "Database unavailable at startup (%s: %s). "
            "Endpoints will return 503 until the DB is reachable. "
            "Check DATABASE_URL and Tailscale connectivity.",
            type(exc).__name__,
            exc,
        )
    yield
    # Shutdown
    await embed.close_client()
    await db.close_pool()
    logger.info("Service stopped")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="nl2sql RAG service",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/cache/stats", tags=["ops"])
async def cache_stats_endpoint(request: Request) -> dict[str, int]:
    stats = cache.cache_stats()
    pool = request.app.state.pool
    if pool is not None:
        try:
            stats.update(await db.get_query_cache_stats(pool))
        except Exception:  # noqa: BLE001
            logger.exception("Failed to load DB query cache stats")
    return stats


@app.post("/cache/clear", tags=["ops"])
async def cache_clear_endpoint(request: Request) -> dict[str, int]:
    cleared = cache.clear_memory_caches()
    pool = request.app.state.pool
    if pool is not None:
        try:
            cleared["db_query_cache_cleared"] = await db.clear_query_cache(pool)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to clear DB query cache")
    return cleared


@app.get("/governance/rules", tags=["ops"])
async def governance_rules_endpoint() -> dict[str, object]:
    _ensure_governance_enabled()
    config = get_config(settings)
    enabled_lookup = {rule.name for rule in get_active_rules(config)}
    return {
        "total_rules": len(RULES),
        "enabled_rules": len(enabled_lookup),
        "governance_enabled": settings.governance_enabled,
        "rules": [
            {
                "name": rule.name,
                "category": rule.category,
                "severity": rule.severity,
                "enabled": rule.name in enabled_lookup,
                "description": rule.description,
            }
            for rule in RULES
        ],
    }


@app.post("/governance/validate", tags=["ops"])
async def governance_validate_endpoint(
    payload: dict[str, object],
) -> dict[str, object]:
    _ensure_governance_enabled()

    sql = payload.get("sql")
    query = payload.get("query")
    tables_in_scope_raw = payload.get("tables_in_scope", [])

    if not isinstance(sql, str) or not sql.strip():
        raise HTTPException(status_code=422, detail="Field 'sql' must be a non-empty string")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=422, detail="Field 'query' must be a non-empty string")
    if tables_in_scope_raw is None:
        tables_in_scope: list[str] = []
    elif isinstance(tables_in_scope_raw, list) and all(
        isinstance(item, str) for item in tables_in_scope_raw
    ):
        tables_in_scope = [item.strip() for item in tables_in_scope_raw if item.strip()]
    else:
        raise HTTPException(status_code=422, detail="Field 'tables_in_scope' must be a list of strings")

    allowed_columns = await load_columns_for_tables(
        tables=tables_in_scope,
        settings=settings,
    )
    passes, violations = await review_sql(
        sql=sql,
        query=query,
        tables_in_scope=tables_in_scope,
        allowed_columns=allowed_columns,
        settings=settings,
    )
    return {
        "passes": passes,
        "violations": violations,
        "sql": sql,
        "query": query,
    }


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(EmbeddingTimeoutError)
@app.exception_handler(EmbeddingUpstreamError)
async def _handle_upstream_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Embedding upstream error: %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(EmbeddingClientError)
@app.exception_handler(EmbeddingDimensionError)
async def _handle_client_error(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("Embedding client/dimension error: %s", exc)
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(asyncpg.PostgresConnectionError)
@app.exception_handler(asyncpg.CannotConnectNowError)
@app.exception_handler(OSError)
async def _handle_db_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Database unavailable: %s", exc)
    request.app.state.pool = None
    try:
        await db.close_pool()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to close DB pool after connection error")
    return JSONResponse(status_code=503, content={"detail": "Database unavailable. Try again later."})


# ---------------------------------------------------------------------------
# Help endpoints
# ---------------------------------------------------------------------------


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@app.get("/help", response_class=HTMLResponse, include_in_schema=False)
async def help_index_endpoint(request: Request) -> HTMLResponse:
    """Render the public in-app route documentation hub."""
    html = help_docs.render_index_page(request.app.openapi(), _base_url(request))
    return HTMLResponse(html)


@app.get("/help/{module}", response_class=HTMLResponse, include_in_schema=False)
async def help_module_endpoint(request: Request, module: str) -> HTMLResponse:
    """Render a module-specific route documentation page."""
    html = help_docs.render_module_page(module, request.app.openapi(), _base_url(request))
    if html is None:
        raise HTTPException(status_code=404, detail="Help module not found")
    return HTMLResponse(html)


@app.get("/help/{module}/{route_slug}", response_class=HTMLResponse, include_in_schema=False)
async def help_detail_endpoint(request: Request, module: str, route_slug: str) -> HTMLResponse:
    """Render detailed documentation for a single route."""
    html = help_docs.render_detail_page(module, route_slug, request.app.openapi(), _base_url(request))
    if html is None:
        raise HTTPException(status_code=404, detail="Help route not found")
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _try_reconnect_pool(request: Request) -> asyncpg.Pool | None:
    app = request.app
    pool: asyncpg.Pool | None = app.state.pool
    if pool is not None:
        return pool

    if not hasattr(app.state, "pool_reconnect_lock"):
        app.state.pool_reconnect_lock = asyncio.Lock()
    if not hasattr(app.state, "pool_last_reconnect_attempt"):
        app.state.pool_last_reconnect_attempt = 0.0

    now = time.monotonic()
    if now - app.state.pool_last_reconnect_attempt < settings.db_reconnect_min_interval:
        return None

    async with app.state.pool_reconnect_lock:
        pool = app.state.pool
        if pool is not None:
            return pool

        now = time.monotonic()
        if now - app.state.pool_last_reconnect_attempt < settings.db_reconnect_min_interval:
            return None
        app.state.pool_last_reconnect_attempt = now

        try:
            pool = await db.create_pool()
            await db.bootstrap(pool)
            await ingest.ensure_hnsw_index(pool)
            app.state.pool = pool
            logger.info("Database reconnect succeeded; pool restored")
            return pool
        except Exception as exc:  # noqa: BLE001
            app.state.pool = None
            logger.warning("Database reconnect failed (%s: %s)", type(exc).__name__, exc)
            return None


async def _require_pool(request: Request) -> asyncpg.Pool:
    """Return the pool or raise a 503 if the DB is unavailable."""
    pool: asyncpg.Pool | None = request.app.state.pool
    if pool is None:
        pool = await _try_reconnect_pool(request)
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Database unavailable. Check DATABASE_URL and Tailscale connectivity. "
                "The service will retry connection automatically."
            ),
        )
    return pool


@app.get("/health", tags=["ops"])
async def health(request: Request) -> dict:
    if request.app.state.pool is None:
        await _try_reconnect_pool(request)
    db_status = "unavailable"
    if request.app.state.pool is not None:
        try:
            await asyncio.wait_for(request.app.state.pool.execute("SELECT 1"), timeout=3)
            db_status = "connected"
        except Exception:
            db_status = "unreachable"
    return {"status": "ok", "db": db_status}


@app.get("/telemetry/recent", tags=["ops"])
async def telemetry_recent_endpoint(
    request: Request,
    limit: int = 50,
    endpoint: str | None = None,
) -> dict[str, object]:
    """Return recent request telemetry events for quick operational debugging."""
    pool = await _require_pool(request)
    results = await db.list_recent_request_events(pool, limit=limit, endpoint=endpoint)
    return {"results": results}


@app.get("/telemetry/summary", tags=["ops"])
async def telemetry_summary_endpoint(
    request: Request,
    endpoint: str | None = None,
    since_minutes: int = 1440,
) -> dict[str, object]:
    """Return aggregate telemetry KPIs for monitoring and release gating."""
    pool = await _require_pool(request)
    summary = await db.get_telemetry_summary(
        pool,
        endpoint=endpoint,
        since_minutes=since_minutes,
    )
    summary["endpoint"] = endpoint
    summary["since_minutes"] = since_minutes
    return summary


@app.post("/benchmark/cases", response_model=BenchmarkCaseCreateResponse, tags=["ops"])
async def benchmark_add_case_endpoint(
    request: Request,
    body: BenchmarkCaseCreateRequest,
) -> BenchmarkCaseCreateResponse:
    """Persist a benchmark case for replay and regression gating."""
    pool = await _require_pool(request)
    case_id = await db.insert_benchmark_case(
        pool,
        query_text=body.query,
        gold_sql=body.gold_sql,
        expected_status=body.expected_status,
        slices=body.slices,
        error_label=body.error_label,
        source=body.source,
        metadata=body.metadata,
    )
    return BenchmarkCaseCreateResponse(
        id=case_id,
        query=body.query,
        expected_status=body.expected_status,
    )


@app.get("/benchmark/cases", tags=["ops"])
async def benchmark_list_cases_endpoint(
    request: Request,
    limit: int = 100,
    active_only: bool = True,
) -> dict[str, object]:
    """List benchmark cases ordered by newest first."""
    pool = await _require_pool(request)
    results = await db.list_benchmark_cases(pool, limit=limit, active_only=active_only)
    return {"results": results}


@app.get("/ingest/groups/status", response_model=GroupEmbeddingStatusResponse, tags=["ingestion"])
async def ingest_groups_status_endpoint(request: Request) -> GroupEmbeddingStatusResponse:
    """
    Return current-vs-embedded schema_version comparison per schema group.

    Costs one DB query + file reads. No embed call made.
    Use this to check whether re-ingesting is needed after a rag_schema/ update.
    """
    pool = await _require_pool(request)
    stored_rows = await db.get_group_embedding_status(pool)
    stored_by_source = {row["source"]: row for row in stored_rows}

    entities = schema_loader.load_entities()
    items = []
    current_count = 0
    stale_count = 0
    never_embedded_count = 0

    for entity in entities:
        entity_id: str = entity.get("entity_id", "")
        group_name: str = entity.get("chunk_group_name", "")
        try:
            file_hash = schema_loader.get_schema_version(entity_id)
        except KeyError:
            file_hash = ""

        stored = stored_by_source.get(group_name)
        stored_version: str | None = stored["stored_version"] if stored else None
        last_embedded_at = str(stored["last_embedded_at"]) if stored and stored["last_embedded_at"] else None

        if stored is None:
            never_embedded_count += 1
            is_current = False
        elif stored_version == file_hash:
            current_count += 1
            is_current = True
        else:
            stale_count += 1
            is_current = False

        items.append({
            "group_name": group_name,
            "entity_id": entity_id,
            "file_hash": file_hash,
            "stored_version": stored_version,
            "is_current": is_current,
            "last_embedded_at": last_embedded_at,
        })

    return GroupEmbeddingStatusResponse(
        groups=items,
        current_count=current_count,
        stale_count=stale_count,
        never_embedded_count=never_embedded_count,
    )


@app.post("/ingest", response_model=IngestResponse, tags=["ingestion"])
async def ingest_endpoint(
    request: Request,
    body: Annotated[Union[IngestTextRequest, IngestSchemaRequest], IngestRequest],
) -> IngestResponse:
    pool = await _require_pool(request)

    if body.type == "text":
        inserted = await ingest.ingest_text(body.text, body.source, pool)
    else:
        inserted = await ingest.ingest_schema(body.tables, body.source, pool)

    return IngestResponse(inserted=inserted, updated=0, source=body.source)


@app.post("/query", response_model=QueryResponse, tags=["retrieval"])
async def query_endpoint(request: Request, body: QueryRequest) -> QueryResponse:
    pool = await _require_pool(request)
    top_k = body.top_k if body.top_k is not None else settings.top_k
    search_query = await query_rewriter.rewrite_search_query(body.query, pool, settings)
    results = await retrieve.retrieve(body.query, top_k, pool, search_query=search_query)
    return QueryResponse(results=results)


@app.post("/ingest/groups", response_model=IngestGroupsResponse, tags=["ingestion"])
async def ingest_groups_endpoint(
    request: Request,
    body: IngestGroupsRequest,
) -> IngestGroupsResponse:
    """Embed and store a list of schema-group chunks with ``metadata.type='schema_group'``."""
    pool = await _require_pool(request)
    if body.group_names is None:
        counts = await ingest.ingest_schema_groups(None, pool)
        source = "all groups"
    else:
        counts = await ingest.ingest_schema_groups(body.group_names, pool)
        source = ", ".join(body.group_names)
    if counts["inserted_count"] + counts["updated_count"] > 0:
        await _invalidate_query_caches(pool)
    return IngestGroupsResponse(
        inserted=counts["inserted_count"],
        updated=counts["updated_count"],
        skipped=counts.get("skipped_count", 0),
        source=source,
        enrichment_summary=counts.get("enrichment_summary"),
        failed_groups=counts.get("failed_groups", []),
        failure_count=len(counts.get("failed_groups", [])),
    )


@app.post("/ingest/knowledge", response_model=IngestResponse, tags=["ingestion"])
async def ingest_knowledge_endpoint(
    request: Request,
    body: IngestKnowledgeRequest,
) -> IngestResponse:
    """Embed all rag_schema knowledge sources: columns, SQL examples, relations, graph nodes, view registry, and schema rules."""
    pool = await _require_pool(request)
    counts = await ingest.ingest_enriched_knowledge(
        include_column_catalog=body.include_column_catalog,
        include_sql_examples=body.include_sql_examples,
        include_relations=body.include_relations,
        include_graph=body.include_graph,
        include_view_registry=body.include_view_registry,
        include_onboarding_rules=body.include_onboarding_rules,
        column_limit=body.column_limit,
        sql_example_limit=body.sql_example_limit,
        relation_limit=body.relation_limit,
        graph_limit=body.graph_limit,
        view_registry_limit=body.view_registry_limit,
        pool=pool,
    )
    if counts["inserted_count"] + counts["updated_count"] > 0:
        await _invalidate_query_caches(pool)
    return IngestResponse(
        inserted=counts["inserted_count"],
        updated=counts["updated_count"],
        skipped=counts.get("skipped_count", 0),
        source="knowledge",
    )


@app.post("/ingest/patterns", response_model=EmbeddedIngestResponse, tags=["ingestion"])
async def ingest_patterns_endpoint(request: Request) -> EmbeddedIngestResponse:
    pool = await _require_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                query_text,
                sql_used,
                tables_used,
                join_conditions,
                matched_groups,
                use_count,
                last_used_at,
                created_at
            FROM nl2sql_learned_patterns
            WHERE is_active = TRUE
              AND use_count >= $1
            ORDER BY use_count DESC, last_used_at DESC
            """,
            settings.min_pattern_use_count,
        )

    chunks: list[dict] = []
    for row in rows:
        pattern = {
            "id": row["id"],
            "query_text": row["query_text"],
            "sql_used": row["sql_used"],
            "tables_used": list(row["tables_used"] or []),
            "join_conditions": _coerce_json(row["join_conditions"], default=[]),
            "matched_groups": list(row["matched_groups"] or []),
            "use_count": row["use_count"],
            "last_used_at": row["last_used_at"],
            "created_at": row["created_at"],
        }
        content = pattern_store.format_patterns_for_prompt([pattern])
        schema_version = hashlib.md5(content.encode()).hexdigest()[:8]
        chunks.append(
            {
                "text": content,
                "source": f"learned_pattern_{pattern['id']}",
                "chunk_index": 0,
                "token_count": chunker.count_tokens(content),
                "embedding_model": settings.embedding_model,
                "type": "learned_pattern",
                "tables": pattern["tables_used"],
                "join_conditions": pattern["join_conditions"],
                "use_count": pattern["use_count"],
                "pattern_id": pattern["id"],
                "schema_version": schema_version,
            }
        )

    counts = await ingest._upsert_versioned_chunks(chunks, pool)
    if counts["inserted_count"] + counts["updated_count"] > 0:
        await _invalidate_query_caches(pool)
    return EmbeddedIngestResponse(
        inserted=counts["inserted_count"],
        updated=counts["updated_count"],
        skipped=counts.get("skipped_count", 0),
        embedded=counts["inserted_count"] + counts["updated_count"],
        source="learned_patterns",
    )


@app.post("/ingest/instructions", response_model=EmbeddedIngestResponse, tags=["ingestion"])
async def ingest_instructions_endpoint(request: Request) -> EmbeddedIngestResponse:
    pool = await _require_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                instruction_type,
                content,
                embedding_source,
                tables_affected,
                confidence_score,
                is_verified
            FROM nl2sql_user_instructions
            WHERE is_active = TRUE
              AND confidence_score >= $1
            ORDER BY is_verified DESC, confidence_score DESC, use_count DESC
            """,
            settings.min_instruction_confidence,
        )

    chunks: list[dict] = []
    for row in rows:
        content = row["embedding_source"]
        schema_version = hashlib.md5(content.encode()).hexdigest()[:8]
        chunks.append(
            {
                "text": content,
                "source": f"user_instruction_{row['id']}",
                "chunk_index": 0,
                "token_count": chunker.count_tokens(content),
                "embedding_model": settings.embedding_model,
                "type": "user_instruction",
                "instruction_type": row["instruction_type"],
                "tables": list(row["tables_affected"] or []),
                "confidence_score": float(row["confidence_score"]),
                "is_verified": bool(row["is_verified"]),
                "is_active": True,
                "instruction_id": row["id"],
                "schema_version": schema_version,
            }
        )

    counts = await ingest._upsert_versioned_chunks(chunks, pool)
    if counts["inserted_count"] + counts["updated_count"] > 0:
        await _invalidate_query_caches(pool)
    return EmbeddedIngestResponse(
        inserted=counts["inserted_count"],
        updated=counts["updated_count"],
        skipped=counts.get("skipped_count", 0),
        embedded=counts["inserted_count"] + counts["updated_count"],
        source="user_instructions",
    )


@app.post("/patterns/feedback", tags=["learning"])
async def patterns_feedback_endpoint(
    request: Request,
    body: PatternFeedbackRequest,
) -> dict[str, int | str]:
    pool = await _require_pool(request)
    async with pool.acquire() as conn:
        if body.helpful:
            row = await conn.fetchrow(
                """
                UPDATE nl2sql_learned_patterns
                SET use_count = use_count + 2,
                    last_used_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                body.pattern_id,
            )
            action = "boosted"
        else:
            row = await conn.fetchrow(
                """
                UPDATE nl2sql_learned_patterns
                SET is_active = FALSE,
                    last_used_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                body.pattern_id,
            )
            action = "deactivated"

    if row is None:
        raise HTTPException(status_code=404, detail="Pattern not found")
    return {"pattern_id": body.pattern_id, "action": action}


@app.post("/teach", response_model=TeachResponse, tags=["learning"])
async def teach_endpoint(
    request: Request,
    body: TeachRequest,
) -> TeachResponse:
    pool = await _require_pool(request)
    try:
        response = await process_teach_request(body, pool)
        if _teach_mutates_cache(response):
            await _invalidate_query_caches(pool)
        return response
    except HTTPException:
        raise
    except (
        asyncpg.PostgresConnectionError,
        asyncpg.CannotConnectNowError,
        OSError,
    ):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Teach endpoint failed: %s", exc)
        return TeachResponse(
            learning_status=LearningStatus.REJECTED,
            message=f"I could not process this instruction: {exc}",
        )


@app.post("/teach/confirm", response_model=TeachResponse, tags=["learning"])
async def teach_confirm_endpoint(
    request: Request,
    body: ConfirmRequest,
) -> TeachResponse:
    pool = await _require_pool(request)
    try:
        response = await process_confirmation(
            token=body.confirmation_token,
            action=body.action,
            pool=pool,
        )
        if _teach_mutates_cache(response):
            await _invalidate_query_caches(pool)
        return response
    except HTTPException:
        raise
    except (
        asyncpg.PostgresConnectionError,
        asyncpg.CannotConnectNowError,
        OSError,
    ):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Teach confirmation endpoint failed: %s", exc)
        return TeachResponse(
            learning_status=LearningStatus.REJECTED,
            message=f"I could not process this confirmation: {exc}",
        )


@app.get("/instructions", tags=["learning"])
async def list_instructions_endpoint(
    request: Request,
    instruction_type: InstructionType | None = None,
    active_only: bool = True,
) -> list[dict]:
    pool = await _require_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                instruction_type,
                content,
                tables_affected,
                confidence_score,
                is_verified,
                is_active,
                use_count,
                success_count,
                failure_count,
                last_used_at,
                created_at
            FROM nl2sql_user_instructions
            WHERE ($1::text IS NULL OR instruction_type = $1)
              AND ($2::bool = FALSE OR is_active = TRUE)
            ORDER BY is_active DESC, is_verified DESC, confidence_score DESC, id DESC
            """,
            instruction_type.value if instruction_type else None,
            active_only,
        )

    return [
        {
            "id": row["id"],
            "instruction_type": row["instruction_type"],
            "content": row["content"],
            "tables_affected": list(row["tables_affected"] or []),
            "confidence_score": float(row["confidence_score"]),
            "is_verified": bool(row["is_verified"]),
            "is_active": bool(row["is_active"]),
            "use_count": row["use_count"],
            "success_count": row["success_count"],
            "failure_count": row["failure_count"],
            "last_used_at": row["last_used_at"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


@app.delete("/instructions/{instruction_id}", tags=["learning"])
async def delete_instruction_endpoint(
    request: Request,
    instruction_id: int,
) -> dict[str, bool | int]:
    pool = await _require_pool(request)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE nl2sql_user_instructions
            SET is_active = FALSE,
                updated_at = NOW()
            WHERE id = $1
            """,
            instruction_id,
        )
        await conn.execute(
            """
            UPDATE nl2sql_embeddings
            SET metadata = jsonb_set(metadata, '{is_active}', 'false'::jsonb, true)
            WHERE source = $1
            """,
            f"user_instruction_{instruction_id}",
        )
    return {"deactivated": True, "instruction_id": instruction_id}


@app.post("/query/groups", response_model=GroupQueryResponse, tags=["retrieval"])
async def query_groups_endpoint(
    request: Request,
    body: QueryRequest,
) -> GroupQueryResponse:
    """Return the closest schema-group chunks and a ready-to-use context block."""
    pool = await _require_pool(request)
    top_k = body.top_k if body.top_k is not None else settings.top_k
    search_query = await query_rewriter.rewrite_search_query(body.query, pool, settings)
    return await retrieve.retrieve_groups(
        body.query,
        top_k,
        pool,
        search_query=search_query,
    )


@app.post("/generate-sql", response_model=GenerateSqlResponse, tags=["generation"])
async def generate_sql_endpoint(
    http_request: Request,
    request: GenerateSqlRequest,
) -> GenerateSqlResponse:
    pool = await _require_pool(http_request)
    top_k = request.top_k if request.top_k else settings.top_k
    request_id = _resolve_request_id(request.request_id)
    set_request_id(request_id)
    started = time.monotonic()
    result = await generate_sql(request.query, pool, settings, top_k)
    asyncio.create_task(
        _log_request_event(
            pool,
            request_id=request_id,
            endpoint="/generate-sql",
            query_text=request.query,
            top_k=top_k,
            status=result.status,
            attempt_count=getattr(result, "attempt_count", None),
            latency_ms=_elapsed_ms(started),
            stage_latencies_ms={},
            warning_codes=[warning.code.value for warning in getattr(result, "warnings", [])],
            error_source=_derive_error_source(getattr(result, "warnings", [])),
            metadata=_generation_metadata(result),
        )
    )
    return result


@app.post("/ask", response_model=AskResponse, tags=["generation"])
async def ask_endpoint(
    http_request: Request,
    request: AskRequest,
) -> AskResponse:
    pool = await _require_pool(http_request)
    top_k = request.top_k if request.top_k else settings.top_k
    request_id = _resolve_request_id(request.request_id)
    set_request_id(request_id)
    started = time.monotonic()
    cache_epoch: int | None = None
    query_embedding: list[float] | None = None

    # --- Ask cache: exact match -------------------------------------------------
    if settings.ask_cache_enabled:
        cached_ask = ask_cache.get_exact(request.query, top_k)
        if cached_ask:
            cached_ask.pop("_top_k", None)
            cached_ask["cache_hit"] = True
            cached_ask["cache_source"] = CacheSource.MEMORY_EXACT.value
            return _ask_success_from_cache(cached_ask)

    # --- Ask cache: semantic match -----------------------------------------------
    if settings.ask_cache_enabled:
        try:
            query_embedding = await _load_query_embedding(request.query)
            if query_embedding is None:
                raise ValueError("query embedding unavailable")
            sem_ask = ask_cache.get_semantic(
                query_embedding,
                top_k,
                threshold=settings.ask_cache_semantic_threshold,
            )
            if sem_ask:
                sem_ask.pop("_top_k", None)
                sem_ask["cache_hit"] = True
                sem_ask["cache_source"] = CacheSource.MEMORY_SEMANTIC.value
                return _ask_success_from_cache(sem_ask)
        except Exception:
            pass  # semantic lookup is best-effort

    if settings.ask_cache_enabled:
        cache_epoch = await db.get_query_cache_epoch(pool)
        cached_ask = await db.get_query_cache_exact(
            pool,
            endpoint="ask",
            query_text=request.query,
            top_k=top_k,
            cache_epoch=cache_epoch,
        )
        if cached_ask:
            ask_cache.set(request.query, top_k, cached_ask, embedding=query_embedding)
            cached_ask["cache_hit"] = True
            cached_ask["cache_source"] = CacheSource.DB_EXACT.value
            return _ask_success_from_cache(cached_ask)

        if query_embedding is not None:
            try:
                sem_ask = await db.get_query_cache_semantic(
                    pool,
                    endpoint="ask",
                    query_embedding=query_embedding,
                    top_k=top_k,
                    cache_epoch=cache_epoch,
                    min_similarity=settings.ask_cache_semantic_threshold,
                )
                if sem_ask:
                    ask_cache.set(request.query, top_k, sem_ask, embedding=query_embedding)
                    sem_ask["cache_hit"] = True
                    sem_ask["cache_source"] = CacheSource.DB_SEMANTIC.value
                    return _ask_success_from_cache(sem_ask)
            except Exception:
                logger.exception("Failed semantic DB ask cache lookup")

    try:
        return await asyncio.wait_for(
            _run_ask_workflow(
                request=request,
                pool=pool,
                top_k=top_k,
                request_id=request_id,
                started=started,
                cache_epoch=cache_epoch,
                query_embedding=query_embedding,
            ),
            timeout=settings.ask_timeout,
        )
    except asyncio.TimeoutError:
        response = AskRejected(
            sql=None,
            warnings=[
                SqlWarning(
                    code=WarningCode.REQUEST_TIMEOUT,
                    message=(
                        "Ask workflow exceeded the service time budget "
                        f"of {settings.ask_timeout}s."
                    ),
                )
            ],
            attempt_count=0,
            react_trace=None,
        )
        asyncio.create_task(
            _log_request_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                top_k=top_k,
                status=response.status,
                attempt_count=response.attempt_count,
                latency_ms=_elapsed_ms(started),
                stage_latencies_ms={},
                warning_codes=[warning.code.value for warning in response.warnings],
                error_source=_derive_error_source(response.warnings),
                metadata={"sql_present": False},
            )
        )
        return response


async def _run_ask_workflow(
    request: AskRequest,
    pool: asyncpg.Pool,
    top_k: int,
    request_id: str,
    started: float,
    cache_epoch: int | None = None,
    query_embedding: list[float] | None = None,
) -> AskResponse:
    stage_latencies_ms: dict[str, int] = {}

    sql_started = time.monotonic()
    sql_result = await generate_sql(request.query, pool, settings, top_k)
    stage_latencies_ms["sql_generation"] = _elapsed_ms(sql_started)
    if sql_result.status == "clarification_needed":
        asyncio.create_task(
            _log_request_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                top_k=top_k,
                status=sql_result.status,
                attempt_count=None,
                latency_ms=_elapsed_ms(started),
                stage_latencies_ms=stage_latencies_ms,
                warning_codes=[],
                error_source="clarification",
                metadata={
                    "failure_reason": sql_result.failure_reason,
                    "suggestion_count": len(sql_result.suggestions),
                },
            )
        )
        return sql_result
    if sql_result.status == "rejected":
        response = AskRejected(
            sql=None,
            warnings=sql_result.warnings,
            attempt_count=sql_result.attempt_count,
            cache_hit=False,
            cache_source=CacheSource.NONE,
            react_trace=sql_result.react_trace,
        )
        asyncio.create_task(
            _log_request_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                top_k=top_k,
                status=response.status,
                attempt_count=response.attempt_count,
                latency_ms=_elapsed_ms(started),
                stage_latencies_ms=stage_latencies_ms,
                warning_codes=[warning.code.value for warning in response.warnings],
                error_source=_derive_error_source(response.warnings),
                metadata={
                    "sql_present": response.sql is not None,
                    "tables_used": [],
                    "matched_groups": [],
                },
            )
        )
        return response

    capped_sql = mysql_executor.apply_row_cap(sql_result.sql, cap=50)
    execution_started = time.monotonic()
    columns, rows, execution_warnings = await mysql_executor.execute_sql(
        sql=capped_sql,
        settings=settings,
    )
    stage_latencies_ms["execution"] = _elapsed_ms(execution_started)
    if execution_warnings:
        response = AskRejected(
            sql=capped_sql,
            warnings=[*sql_result.warnings, *execution_warnings],
            attempt_count=sql_result.attempt_count,
            cache_hit=False,
            cache_source=CacheSource.NONE,
            react_trace=sql_result.react_trace,
        )
        asyncio.create_task(
            _log_request_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                top_k=top_k,
                status=response.status,
                attempt_count=response.attempt_count,
                latency_ms=_elapsed_ms(started),
                stage_latencies_ms=stage_latencies_ms,
                warning_codes=[warning.code.value for warning in response.warnings],
                error_source=_derive_error_source(response.warnings),
                metadata={
                    "sql_present": response.sql is not None,
                    "tables_used": sql_result.tables_used,
                    "matched_groups": sql_result.matched_groups,
                },
            )
        )
        return response

    answer_started = time.monotonic()
    if "deterministic_payment" in sql_result.matched_groups:
        answer_text = answer_generator.build_fallback_answer(
            query=request.query,
            columns=columns,
            rows=rows,
            row_count=len(rows),
        )
        answer_warnings = []
    else:
        answer_text, answer_warnings = await answer_generator.generate_answer(
            query=request.query,
            sql=capped_sql,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            sql_warnings=[*sql_result.warnings, *execution_warnings],
            settings=settings,
        )
    stage_latencies_ms["answer_generation"] = _elapsed_ms(answer_started)
    if answer_text is None:
        enriched_answer_warnings: list[SqlWarning] = [
            SqlWarning(
                code=warning.code,
                message=(
                    f"{warning.message} | Execution metadata: "
                    f"row_count={len(rows)}, columns={columns}"
                ),
            )
            for warning in answer_warnings
        ]
        if not enriched_answer_warnings:
            enriched_answer_warnings = [
                SqlWarning(
                    code=WarningCode.ANSWER_MALFORMED,
                    message=(
                        "Answer generation failed | Execution metadata: "
                        f"row_count={len(rows)}, columns={columns}"
                    ),
                )
            ]
        response = AskRejected(
            sql=capped_sql,
            warnings=[*sql_result.warnings, *enriched_answer_warnings],
            attempt_count=sql_result.attempt_count,
            cache_hit=False,
            cache_source=CacheSource.NONE,
            react_trace=sql_result.react_trace,
        )
        asyncio.create_task(
            _log_request_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                top_k=top_k,
                status=response.status,
                attempt_count=response.attempt_count,
                latency_ms=_elapsed_ms(started),
                stage_latencies_ms=stage_latencies_ms,
                warning_codes=[warning.code.value for warning in response.warnings],
                error_source=_derive_error_source(response.warnings),
                metadata={
                    "sql_present": response.sql is not None,
                    "row_count": len(rows),
                    "tables_used": sql_result.tables_used,
                    "matched_groups": sql_result.matched_groups,
                },
            )
        )
        return response

    if rows:
        asyncio.create_task(
            pattern_store.save_pattern(
                query_text=request.query,
                sql=capped_sql,
                tables_used=sql_result.tables_used,
                matched_groups=sql_result.matched_groups,
                pool=pool,
            )
        )

    response = AskSuccess(
        answer=answer_text,
        sql=capped_sql,
        warnings=[*sql_result.warnings, *execution_warnings, *answer_warnings],
        row_count=len(rows),
        columns=columns,
        tables_used=sql_result.tables_used,
        matched_groups=sql_result.matched_groups,
        attempt_count=sql_result.attempt_count,
        cache_hit=False,
        cache_source=CacheSource.NONE,
        react_trace=sql_result.react_trace,
    )
    asyncio.create_task(
        _log_request_event(
            pool,
            request_id=request_id,
            endpoint="/ask",
            query_text=request.query,
            top_k=top_k,
            status=response.status,
            attempt_count=response.attempt_count,
            latency_ms=_elapsed_ms(started),
            stage_latencies_ms=stage_latencies_ms,
            warning_codes=[warning.code.value for warning in response.warnings],
            error_source=_derive_error_source(response.warnings),
            metadata={
                "row_count": response.row_count,
                "tables_used": response.tables_used,
                "matched_groups": response.matched_groups,
            },
        )
    )
    if settings.ask_cache_enabled:
        payload = response.model_dump(mode="json")
        payload["cache_hit"] = False
        payload["cache_source"] = CacheSource.NONE.value
        if query_embedding is None:
            try:
                query_embedding = await _load_query_embedding(request.query)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to load ask cache embedding")
        ask_cache.set(request.query, top_k, payload, embedding=query_embedding)
        try:
            await db.upsert_query_cache_entry(
                pool,
                endpoint="ask",
                query_text=request.query,
                top_k=top_k,
                response_json=payload,
                query_embedding=query_embedding,
                cache_epoch=cache_epoch or await db.get_query_cache_epoch(pool),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist ask cache entry")
    return response


def _json_event(event: str, **payload: object) -> str:
    return json.dumps(
        {"event": event, **payload},
        default=str,
        separators=(",", ":"),
    ) + "\n"


def _warning_payload(warnings: list[SqlWarning]) -> list[dict]:
    return [warning.model_dump(mode="json") for warning in warnings]


def _response_payload(response: AskResponse) -> dict:
    return response.model_dump(mode="json")


@app.post("/ask/stream", tags=["generation"])
async def ask_stream_endpoint(
    http_request: Request,
    request: AskRequest,
) -> StreamingResponse:
    pool = await _require_pool(http_request)
    top_k = request.top_k if request.top_k else settings.top_k
    request_id = _resolve_request_id(request.request_id)
    set_request_id(request_id)
    started = time.monotonic()

    async def event_stream() -> AsyncIterator[str]:
        stage_latencies_ms: dict[str, int] = {}
        yield _json_event(
            "started",
            message="Received question.",
            query=request.query,
            top_k=top_k,
            request_id=request_id,
        )

        yield _json_event(
            "sql_generation_started",
            message="Retrieving schema context and generating guarded SQL.",
        )
        sql_started = time.monotonic()
        sql_task = asyncio.create_task(generate_sql(request.query, pool, settings, top_k))
        while True:
            done, _ = await asyncio.wait({sql_task}, timeout=10)
            if done:
                break
            yield _json_event(
                "sql_generation_running",
                message="Still generating and validating SQL.",
            )
        sql_result = await sql_task
        stage_latencies_ms["sql_generation"] = _elapsed_ms(sql_started)
        if sql_result.status == "clarification_needed":
            asyncio.create_task(
                _log_request_event(
                    pool,
                    request_id=request_id,
                    endpoint="/ask/stream",
                    query_text=request.query,
                    top_k=top_k,
                    status=sql_result.status,
                    attempt_count=None,
                    latency_ms=_elapsed_ms(started),
                    stage_latencies_ms=stage_latencies_ms,
                    warning_codes=[],
                    error_source="clarification",
                    metadata={
                        "failure_reason": sql_result.failure_reason,
                        "suggestion_count": len(sql_result.suggestions),
                    },
                )
            )
            yield _json_event(
                "sql_generation_rejected",
                message="SQL generation needs clarification.",
                question=sql_result.question,
                suggestions=sql_result.suggestions,
            )
            yield _json_event("final", response=_response_payload(sql_result))
            return
        if sql_result.status == "rejected":
            response = AskRejected(
                sql=None,
                warnings=sql_result.warnings,
                attempt_count=sql_result.attempt_count,
                react_trace=sql_result.react_trace,
            )
            asyncio.create_task(
                _log_request_event(
                    pool,
                    request_id=request_id,
                    endpoint="/ask/stream",
                    query_text=request.query,
                    top_k=top_k,
                    status=response.status,
                    attempt_count=response.attempt_count,
                    latency_ms=_elapsed_ms(started),
                    stage_latencies_ms=stage_latencies_ms,
                    warning_codes=[warning.code.value for warning in response.warnings],
                    error_source=_derive_error_source(response.warnings),
                    metadata={
                        "sql_present": response.sql is not None,
                        "tables_used": [],
                        "matched_groups": [],
                    },
                )
            )
            yield _json_event(
                "sql_generation_rejected",
                message="SQL generation was rejected by guardrails.",
                warnings=_warning_payload(sql_result.warnings),
                attempt_count=sql_result.attempt_count,
            )
            yield _json_event("final", response=_response_payload(response))
            return

        yield _json_event(
            "sql_generation_finished",
            message="SQL generated and validated.",
            sql=sql_result.sql,
            warnings=_warning_payload(sql_result.warnings),
            tables_used=sql_result.tables_used,
            matched_groups=sql_result.matched_groups,
            attempt_count=sql_result.attempt_count,
        )

        capped_sql = mysql_executor.apply_row_cap(sql_result.sql, cap=50)
        if capped_sql != sql_result.sql:
            yield _json_event(
                "row_cap_applied",
                message="Execution SQL was capped to at most 50 rows.",
                sql=capped_sql,
            )

        yield _json_event(
            "execution_started",
            message="Executing bounded SQL on the app MySQL database.",
        )
        execution_started = time.monotonic()
        columns, rows, execution_warnings = await mysql_executor.execute_sql(
            sql=capped_sql,
            settings=settings,
        )
        stage_latencies_ms["execution"] = _elapsed_ms(execution_started)
        if execution_warnings:
            response = AskRejected(
                sql=capped_sql,
                warnings=[*sql_result.warnings, *execution_warnings],
                attempt_count=sql_result.attempt_count,
                react_trace=sql_result.react_trace,
            )
            asyncio.create_task(
                _log_request_event(
                    pool,
                    request_id=request_id,
                    endpoint="/ask/stream",
                    query_text=request.query,
                    top_k=top_k,
                    status=response.status,
                    attempt_count=response.attempt_count,
                    latency_ms=_elapsed_ms(started),
                    stage_latencies_ms=stage_latencies_ms,
                    warning_codes=[warning.code.value for warning in response.warnings],
                    error_source=_derive_error_source(response.warnings),
                    metadata={
                        "sql_present": response.sql is not None,
                        "tables_used": sql_result.tables_used,
                        "matched_groups": sql_result.matched_groups,
                    },
                )
            )
            yield _json_event(
                "execution_failed",
                message="MySQL execution failed.",
                warnings=_warning_payload(execution_warnings),
            )
            yield _json_event("final", response=_response_payload(response))
            return

        yield _json_event(
            "execution_finished",
            message="SQL execution finished.",
            row_count=len(rows),
            columns=columns,
        )

        yield _json_event(
            "answer_generation_started",
            message="Generating final answer from bounded result rows.",
        )
        answer_started = time.monotonic()
        answer_task = asyncio.create_task(
            answer_generator.generate_answer(
                query=request.query,
                sql=capped_sql,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                sql_warnings=[*sql_result.warnings, *execution_warnings],
                settings=settings,
            )
        )
        while True:
            done, _ = await asyncio.wait({answer_task}, timeout=10)
            if done:
                break
            yield _json_event(
                "answer_generation_running",
                message="Still generating final answer.",
            )
        answer_text, answer_warnings = await answer_task
        stage_latencies_ms["answer_generation"] = _elapsed_ms(answer_started)
        if answer_text is None:
            enriched_answer_warnings: list[SqlWarning] = [
                SqlWarning(
                    code=warning.code,
                    message=(
                        f"{warning.message} | Execution metadata: "
                        f"row_count={len(rows)}, columns={columns}"
                    ),
                )
                for warning in answer_warnings
            ]
            if not enriched_answer_warnings:
                enriched_answer_warnings = [
                    SqlWarning(
                        code=WarningCode.ANSWER_MALFORMED,
                        message=(
                            "Answer generation failed | Execution metadata: "
                            f"row_count={len(rows)}, columns={columns}"
                        ),
                    )
                ]
            response = AskRejected(
                sql=capped_sql,
                warnings=[*sql_result.warnings, *enriched_answer_warnings],
                attempt_count=sql_result.attempt_count,
                react_trace=sql_result.react_trace,
            )
            asyncio.create_task(
                _log_request_event(
                    pool,
                    request_id=request_id,
                    endpoint="/ask/stream",
                    query_text=request.query,
                    top_k=top_k,
                    status=response.status,
                    attempt_count=response.attempt_count,
                    latency_ms=_elapsed_ms(started),
                    stage_latencies_ms=stage_latencies_ms,
                    warning_codes=[warning.code.value for warning in response.warnings],
                    error_source=_derive_error_source(response.warnings),
                    metadata={
                        "sql_present": response.sql is not None,
                        "row_count": len(rows),
                        "tables_used": sql_result.tables_used,
                        "matched_groups": sql_result.matched_groups,
                    },
                )
            )
            yield _json_event(
                "answer_generation_failed",
                message="Answer generation failed.",
                warnings=_warning_payload(enriched_answer_warnings),
            )
            yield _json_event("final", response=_response_payload(response))
            return

        yield _json_event(
            "answer_generation_finished",
            message="Final answer is ready.",
            warnings=_warning_payload(answer_warnings),
        )
        if rows:
            asyncio.create_task(
                pattern_store.save_pattern(
                    query_text=request.query,
                    sql=capped_sql,
                    tables_used=sql_result.tables_used,
                    matched_groups=sql_result.matched_groups,
                    pool=pool,
                )
            )
        response = AskSuccess(
            answer=answer_text,
            sql=capped_sql,
            warnings=[*sql_result.warnings, *execution_warnings, *answer_warnings],
            row_count=len(rows),
            columns=columns,
            tables_used=sql_result.tables_used,
            matched_groups=sql_result.matched_groups,
            attempt_count=sql_result.attempt_count,
            react_trace=sql_result.react_trace,
        )
        asyncio.create_task(
            _log_request_event(
                pool,
                request_id=request_id,
                endpoint="/ask/stream",
                query_text=request.query,
                top_k=top_k,
                status=response.status,
                attempt_count=response.attempt_count,
                latency_ms=_elapsed_ms(started),
                stage_latencies_ms=stage_latencies_ms,
                warning_codes=[warning.code.value for warning in response.warnings],
                error_source=_derive_error_source(response.warnings),
                metadata={
                    "row_count": response.row_count,
                    "tables_used": response.tables_used,
                    "matched_groups": response.matched_groups,
                },
            )
        )
        yield _json_event("final", response=_response_payload(response))

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
    )


def _coerce_json(value: object, default: object) -> object:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value
