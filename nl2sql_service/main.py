from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Union

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from nl2sql_service import answer_generator, db, embed, ingest, mysql_executor, retrieve
from nl2sql_service.config import settings
from nl2sql_service.embed import (
    EmbeddingClientError,
    EmbeddingDimensionError,
    EmbeddingTimeoutError,
    EmbeddingUpstreamError,
)
from nl2sql_service.models import (
    AskRejected,
    AskRequest,
    AskResponse,
    AskSuccess,
    GenerateSqlRequest,
    GenerateSqlResponse,
    GroupQueryResponse,
    IngestGroupsResponse,
    IngestGroupsRequest,
    IngestKnowledgeRequest,
    IngestRequest,
    IngestResponse,
    IngestSchemaRequest,
    IngestTextRequest,
    QueryRequest,
    QueryResponse,
    SqlWarning,
    WarningCode,
)
from nl2sql_service.sql_generator import generate_sql

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


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
    db_ok = request.app.state.pool is not None
    return {"status": "ok", "db": "connected" if db_ok else "unavailable"}


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
    results = await retrieve.retrieve(body.query, top_k, pool)
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
    return IngestGroupsResponse(
        inserted=counts["inserted_count"],
        updated=counts["updated_count"],
        source=source,
        enrichment_summary=counts.get("enrichment_summary"),
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
    return IngestResponse(
        inserted=counts["inserted_count"],
        updated=counts["updated_count"],
        source="knowledge",
    )


@app.post("/query/groups", response_model=GroupQueryResponse, tags=["retrieval"])
async def query_groups_endpoint(
    request: Request,
    body: QueryRequest,
) -> GroupQueryResponse:
    """Return the closest schema-group chunks and a ready-to-use context block."""
    pool = await _require_pool(request)
    top_k = body.top_k if body.top_k is not None else settings.top_k
    return await retrieve.retrieve_groups(body.query, top_k, pool)


@app.post("/generate-sql", response_model=GenerateSqlResponse, tags=["generation"])
async def generate_sql_endpoint(
    http_request: Request,
    request: GenerateSqlRequest,
) -> GenerateSqlResponse:
    pool = await _require_pool(http_request)
    top_k = request.top_k if request.top_k else settings.top_k
    return await generate_sql(request.query, pool, settings, top_k)


@app.post("/ask", response_model=AskResponse, tags=["generation"])
async def ask_endpoint(
    http_request: Request,
    request: AskRequest,
) -> AskResponse:
    pool = await _require_pool(http_request)
    top_k = request.top_k if request.top_k else settings.top_k

    sql_result = await generate_sql(request.query, pool, settings, top_k)
    if sql_result.status == "rejected":
        return AskRejected(
            sql=None,
            warnings=sql_result.warnings,
            attempt_count=sql_result.attempt_count,
            react_trace=sql_result.react_trace,
        )

    capped_sql = mysql_executor.apply_row_cap(sql_result.sql, cap=50)
    columns, rows, execution_warnings = await mysql_executor.execute_sql(
        sql=capped_sql,
        settings=settings,
    )
    if execution_warnings:
        return AskRejected(
            sql=capped_sql,
            warnings=[*sql_result.warnings, *execution_warnings],
            attempt_count=sql_result.attempt_count,
            react_trace=sql_result.react_trace,
        )

    answer_text, answer_warnings = await answer_generator.generate_answer(
        query=request.query,
        sql=capped_sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        sql_warnings=[*sql_result.warnings, *execution_warnings],
        settings=settings,
    )
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
        return AskRejected(
            sql=capped_sql,
            warnings=[*sql_result.warnings, *enriched_answer_warnings],
            attempt_count=sql_result.attempt_count,
            react_trace=sql_result.react_trace,
        )

    return AskSuccess(
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

    async def event_stream() -> AsyncIterator[str]:
        yield _json_event(
            "started",
            message="Received question.",
            query=request.query,
            top_k=top_k,
        )

        yield _json_event(
            "sql_generation_started",
            message="Retrieving schema context and generating guarded SQL.",
        )
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
        if sql_result.status == "rejected":
            response = AskRejected(
                sql=None,
                warnings=sql_result.warnings,
                attempt_count=sql_result.attempt_count,
                react_trace=sql_result.react_trace,
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
        columns, rows, execution_warnings = await mysql_executor.execute_sql(
            sql=capped_sql,
            settings=settings,
        )
        if execution_warnings:
            response = AskRejected(
                sql=capped_sql,
                warnings=[*sql_result.warnings, *execution_warnings],
                attempt_count=sql_result.attempt_count,
                react_trace=sql_result.react_trace,
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
        yield _json_event("final", response=_response_payload(response))

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
    )
