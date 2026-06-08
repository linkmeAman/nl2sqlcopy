from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import inspect
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Union

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

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
    provider_store,
    query_rewriter,
    retrieve,
    schema_loader,
)
from nl2sql_service.cache import ask_cache
from nl2sql_service.config import settings
from nl2sql_service.key_vault import KeyVaultUnavailableError, decrypt_api_key, is_key_vault_configured
from nl2sql_service.llm.factory import LLMFactory
from nl2sql_service.llm import metrics as llm_metrics
from nl2sql_service.provider_health import list_provider_models, test_provider_connection
from nl2sql_service.embed import (
    EmbeddingClientError,
    EmbeddingDimensionError,
    EmbeddingTimeoutError,
    EmbeddingUpstreamError,
)
from nl2sql_service.instruction_store import (
    build_failure_teach_suggestion,
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
    HumanReviewPrompt,
    InstructionType,
    IngestGroupsResponse,
    IngestGroupsRequest,
    IngestKnowledgeRequest,
    IngestRequest,
    IngestResponse,
    IngestSchemaRequest,
    IngestTextRequest,
    AskModelPatchRequest,
    AskModelSnapshot,
    ActiveModelPatchRequest,
    AddApiKeyRequest,
    ApiKeyRecord,
    ModelRoutingPatchRequest,
    ModelRoutingSnapshot,
    ModelRecord,
    LearningStatus,
    PatternFeedbackRequest,
    ProviderConfig as ProviderConfigResponse,
    ProviderTestResult,
    QueryRequest,
    QueryResponse,
    RegisterModelRequest,
    SqlWarning,
    TeachRequest,
    TeachResponse,
    UpdateModelRequest,
    UpdateProviderRequest,
    WarningCode,
    CreateProviderRequest,
)
from nl2sql_service.column_loader import load_columns_for_tables
from nl2sql_service.roles import ALL_ROLES, GENERATION_ROLES, LLMRole
from nl2sql_service.rulebook import RULES, get_active_rules, get_config
from nl2sql_service.sql_generator import (
    PgVectorStore,
    generate_sql,
    is_deterministic_generation_candidate,
    review_sql,
)
from nl2sql_service.log_config import configure_logging, set_request_id
from nl2sql_service.observability.context import (
    bind_context,
    get_observability_context,
    set_current_trace_recorder,
)
from nl2sql_service.observability.logger import AsyncObservabilityPipeline
from nl2sql_service.observability.metrics import render_metrics
from nl2sql_service.observability.middleware import install_request_middleware
from nl2sql_service.observability.sanitization import sanitize_sql, sanitize_value, stable_hash, summarize_text
from nl2sql_service.observability.schemas import ExecutionTraceEvent, FailureAnalysis
from nl2sql_service.observability.tracing import get_span_ids, setup_tracing, start_span

configure_logging(
    enable_file_logging=settings.observability_file_logging_enabled,
    log_dir=settings.observability_log_dir_path(),
    log_filename=settings.observability_log_file_basename,
    retention_days=settings.observability_log_retention_days,
)
logger = logging.getLogger(__name__)

_LOG_DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _resolve_request_id(candidate: str | None) -> str:
    if candidate and candidate.strip():
        return candidate.strip()
    return uuid.uuid4().hex


def _bind_request_context(
    *,
    request_id: str,
    endpoint: str,
) -> dict[str, str]:
    context = bind_context(request_id=request_id, workflow_id=request_id, endpoint=endpoint)
    return {
        "request_id": context.request_id,
        "trace_id": context.trace_id,
        "workflow_id": context.workflow_id,
    }


def _context_metadata() -> dict[str, str | None]:
    context = get_observability_context()
    return {
        "request_id": context.request_id,
        "trace_id": context.trace_id or None,
        "correlation_id": context.correlation_id or None,
        "session_id": context.session_id or None,
        "workflow_id": context.workflow_id or None,
    }


def _enrich_response_with_context(response: GenerateSqlResponse | AskResponse) -> GenerateSqlResponse | AskResponse:
    return response.model_copy(update=_context_metadata())


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


def _result_value(result: Any, field: str) -> Any:
    if isinstance(result, dict):
        return result[field]
    return getattr(result, field)


def _observability_log_dir() -> Path:
    return settings.observability_log_dir_path()


def _active_log_path() -> Path:
    return _observability_log_dir() / settings.observability_log_file_basename


def _serialize_log_file(path: Path, *, day: str) -> dict[str, object]:
    stat = path.stat()
    return {
        "day": day,
        "file": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "is_active": path == _active_log_path(),
    }


def _resolve_log_file(day: str | None) -> tuple[str, Path]:
    normalized = (day or "current").strip().lower()
    if normalized == "current":
        path = _active_log_path()
        if not path.exists():
            raise HTTPException(status_code=404, detail="Active log file does not exist yet.")
        return "current", path
    if not _LOG_DAY_PATTERN.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="day must be 'current' or YYYY-MM-DD.")
    path = _observability_log_dir() / f"{settings.observability_log_file_basename}.{normalized}"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No log file found for day {normalized}.")
    return normalized, path


def _list_log_files() -> list[dict[str, object]]:
    log_dir = _observability_log_dir()
    files: list[tuple[str, Path]] = []
    active_path = _active_log_path()
    if active_path.exists():
        files.append(("current", active_path))
    prefix = f"{settings.observability_log_file_basename}."
    if log_dir.exists():
        for path in sorted(log_dir.glob(f"{settings.observability_log_file_basename}.*"), reverse=True):
            suffix = path.name.removeprefix(prefix)
            if _LOG_DAY_PATTERN.fullmatch(suffix):
                files.append((suffix, path))
    return [_serialize_log_file(path, day=day) for day, path in files]


def _tail_log_lines(path: Path, *, lines: int) -> list[str]:
    if lines < 1:
        raise HTTPException(status_code=422, detail="lines must be at least 1.")
    buffer: deque[str] = deque(maxlen=min(lines, 5000))
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.rstrip("\n")
            if text:
                buffer.append(text)
    return list(buffer)


async def _stream_log_lines(
    *,
    path: Path,
    day: str,
    backlog: int,
    follow: bool,
    poll_interval_ms: int,
) -> AsyncIterator[str]:
    if backlog < 0:
        raise HTTPException(status_code=422, detail="backlog must be zero or greater.")
    if poll_interval_ms < 200:
        raise HTTPException(status_code=422, detail="poll_interval_ms must be at least 200.")

    if backlog:
        for line in _tail_log_lines(path, lines=backlog):
            yield _json_event("log_line", day=day, file=path.name, line=line)

    if not follow:
        yield _json_event("complete", day=day, file=path.name)
        return

    active_path = _active_log_path()
    cursor = path.stat().st_size if path.exists() else 0
    while True:
        current_path = active_path if day == "current" else path
        if not current_path.exists():
            await asyncio.sleep(poll_interval_ms / 1000)
            continue
        current_size = current_path.stat().st_size
        if cursor > current_size:
            cursor = 0
        if current_size > cursor:
            with current_path.open(encoding="utf-8", errors="replace") as handle:
                handle.seek(cursor)
                for raw_line in handle:
                    line = raw_line.rstrip("\n")
                    if line:
                        yield _json_event("log_line", day=day, file=current_path.name, line=line)
                cursor = handle.tell()
        await asyncio.sleep(poll_interval_ms / 1000)


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
    review_prompt_raw = payload.get("review_prompt")
    review_prompt = (
        HumanReviewPrompt(**review_prompt_raw)
        if isinstance(review_prompt_raw, dict)
        else review_prompt_raw
    )
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
        review_prompt=review_prompt,
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
        metadata.update({key: value for key, value in _context_metadata().items() if value})
        kwargs["warning_codes"] = warning_codes
        kwargs["metadata"] = metadata
        kwargs.setdefault("trace_id", get_observability_context().trace_id or None)
        kwargs.setdefault("correlation_id", get_observability_context().correlation_id or None)
        kwargs.setdefault("session_id", get_observability_context().session_id or None)
        kwargs.setdefault("workflow_id", get_observability_context().workflow_id or None)
        await db.insert_request_event(pool, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write request telemetry")


async def _log_failure_event(
    pool: asyncpg.Pool,
    *,
    request_id: str,
    endpoint: str,
    query_text: str,
    warning_codes: list[str],
    error_source: str | None,
    sql_preview: str | None,
    tables_attempted: list[str],
    latency_ms: int,
    trace_emit=None,
) -> None:
    """Write to the dedicated failure log and attach a teach suggestion."""
    if trace_emit is not None:
        await trace_emit(
            stage="failure_log_write",
            status="started",
            message="Writing NL2SQL failure context.",
            details={
                "endpoint": endpoint,
                "warning_codes": warning_codes,
                "error_source": error_source,
            },
        )
    try:
        suggestion = build_failure_teach_suggestion(
            query=query_text,
            warning_codes=warning_codes,
            tables_used=tables_attempted,
            sql_preview=sql_preview,
        )
        failure_analysis = FailureAnalysis(
            failure_type=error_source or "unknown",
            failed_step=endpoint,
            root_cause="; ".join(warning_codes) if warning_codes else "unknown",
            latency_breakdown={"total_ms": latency_ms},
            recommended_fix="Inspect the trace for provider, retrieval, and validation spans.",
        ).to_dict()
        await db.insert_failure_log(
            pool,
            request_id=request_id,
            trace_id=get_observability_context().trace_id or None,
            correlation_id=get_observability_context().correlation_id or None,
            session_id=get_observability_context().session_id or None,
            workflow_id=get_observability_context().workflow_id or None,
            endpoint=endpoint,
            query_text=query_text,
            warning_codes=warning_codes,
            error_source=error_source,
            failure_type=error_source,
            root_cause=failure_analysis["root_cause"],
            sql_preview=sql_preview,
            tables_attempted=tables_attempted,
            latency_ms=latency_ms,
            suggest_teach=suggestion,
            failure_details=failure_analysis,
        )
        if trace_emit is not None:
            await trace_emit(
                stage="failure_log_write",
                status="completed",
                message="NL2SQL failure context written.",
                details={"endpoint": endpoint},
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write failure log entry")
        if trace_emit is not None:
            await trace_emit(
                stage="failure_log_write",
                status="warning",
                message="Failed to write NL2SQL failure context.",
                error_source="failure_log_write",
                details={"endpoint": endpoint},
            )


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


def _normalize_review_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _review_singularize(term: str) -> str:
    if len(term) > 3 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 3 and term.endswith("ses"):
        return term[:-2]
    if len(term) > 2 and term.endswith("s"):
        return term[:-1]
    return term


def _review_pluralize(term: str) -> str:
    if term.endswith("y") and len(term) > 1 and term[-2] not in "aeiou":
        return term[:-1] + "ies"
    if term.endswith("s"):
        return term + "es"
    return term + "s"


def _review_table_aliases(table: str) -> set[str]:
    normalized = _normalize_review_text(table)
    tokens = [token for token in normalized.split() if token]
    aliases = {normalized}
    aliases.update(tokens)
    aliases.update(_review_singularize(token) for token in tokens)
    aliases.update(_review_pluralize(_review_singularize(token)) for token in tokens)
    if len(tokens) > 1:
        singular_last = _review_singularize(tokens[-1])
        aliases.add(" ".join([*tokens[:-1], singular_last]))
        aliases.add(" ".join([*tokens[:-1], _review_pluralize(singular_last)]))
    return {alias for alias in aliases if alias}


def _query_mentions_table(query: str, table: str) -> bool:
    query_text = f" {_normalize_review_text(query)} "
    return any(f" {alias} " in query_text for alias in _review_table_aliases(table))


def _build_sql_review_prompt(
    *,
    query: str,
    sql: str,
    tables_used: list[str],
) -> HumanReviewPrompt:
    used_table_mentioned = any(_query_mentions_table(query, table) for table in tables_used)
    needs_review = bool(tables_used) and not used_table_mentioned
    reason = None
    if needs_review:
        reason = (
            "The generated table name is not explicitly mentioned in the question. "
            "Review whether the chosen table matches the intended business term."
        )

    table_text = ", ".join(tables_used) if tables_used else "the selected tables"
    content = (
        f"For the query '{query}', verify whether this SQL uses the correct "
        f"tables and business meaning. Current SQL: {sql}"
    )
    if needs_review:
        content = (
            f"For the query '{query}', the generated SQL used {table_text}, "
            "but the user wording did not explicitly mention that table name. "
            f"If this is wrong, replace this correction with the intended table(s), "
            f"columns, or business rule. Incorrect SQL observed: {sql}"
        )

    return HumanReviewPrompt(
        question=(
            "Does this SQL correctly answer the question? "
            "If not, edit and save the correction so future prompts learn it."
        ),
        needs_review=needs_review,
        reason=reason,
        teach_payload={
            "instruction_type": InstructionType.CORRECTION.value,
            "content": content,
            "tables_affected": tables_used,
            "source_query": query,
            "sql_preview": sql,
        },
    )


def _attach_review_prompt(
    result: GenerateSqlResponse,
    query: str,
) -> GenerateSqlResponse:
    if result.status != "ok":
        return result
    return result.model_copy(
        update={
            "review_prompt": _build_sql_review_prompt(
                query=query,
                sql=result.sql,
                tables_used=result.tables_used,
            )
        }
    )


class TraceRecorder:
    """Per-request sanitized trace logger and optional stream queue."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        request_id: str,
        layer: str = "nl2sql-service",
        stream_queue: asyncio.Queue[dict[str, object]] | None = None,
        pipeline: AsyncObservabilityPipeline | None = None,
    ) -> None:
        self.pool = pool
        self.request_id = request_id
        self.layer = layer
        self.stream_queue = stream_queue
        self.pipeline = pipeline
        self.seq = 0
        self._stage_started_at: dict[str, str] = {}

    async def emit(
        self,
        *,
        event: str | None = None,
        stage: str,
        status: str,
        message: str,
        duration_ms: int | None = None,
        warning_codes: list[str] | None = None,
        error_source: str | None = None,
        details: dict[str, object] | None = None,
        provider: str | None = None,
        model: str | None = None,
        retry_count: int = 0,
        reasoning_summary: str | None = None,
        input_summary: dict[str, object] | None = None,
        output_summary: dict[str, object] | None = None,
        token_usage: dict[str, object] | None = None,
        errors: list[str] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.seq += 1
        context = get_observability_context()
        trace_id, span_id = get_span_ids()
        now = datetime.now(timezone.utc).isoformat()
        if status == "started":
            self._stage_started_at[stage] = now
        event_payload = ExecutionTraceEvent(
            request_id=self.request_id,
            trace_id=trace_id or context.trace_id,
            correlation_id=context.correlation_id,
            session_id=context.session_id,
            workflow_id=context.workflow_id,
            layer=self.layer,
            stage=stage,
            status=status,
            message=message,
            seq=self.seq,
            event=event or stage,
            span_id=span_id,
            duration_ms=duration_ms,
            provider=provider,
            model=model,
            retry_count=retry_count,
            reasoning_summary=summarize_text(reasoning_summary),
            input_summary=sanitize_value(input_summary or {}) or {},
            output_summary=sanitize_value(output_summary or {}) or {},
            warning_codes=[str(code) for code in (warning_codes or [])],
            error_source=error_source,
            errors=[str(error) for error in (errors or [])],
            token_usage=sanitize_value(token_usage or {}) or {},
            metadata=sanitize_value(
                {
                    **(metadata or {}),
                    "details": details or {},
                }
            )
            or {},
            started_at=self._stage_started_at.get(stage) if status != "started" else now,
            ended_at=None if status == "started" else now,
            service=settings.observability_service_name,
        ).to_dict()
        if self.pipeline is not None:
            await self.pipeline.publish(event_payload)
        else:
            await _persist_trace_event(self.pool, event_payload)
        if self.stream_queue is not None:
            await self.stream_queue.put(event_payload)


async def _persist_trace_event(pool: asyncpg.Pool, event: dict[str, object]) -> None:
    if not hasattr(pool, "acquire"):
        return
    try:
        await db.insert_trace_event(
            pool,
            request_id=str(event["request_id"]),
            trace_id=str(event.get("trace_id") or "") or None,
            correlation_id=str(event.get("correlation_id") or "") or None,
            session_id=str(event.get("session_id") or "") or None,
            workflow_id=str(event.get("workflow_id") or "") or None,
            seq=int(event["seq"]),
            event=str(event.get("event") or ""),
            layer=str(event["layer"]),
            stage=str(event["stage"]),
            status=str(event["status"]),
            message=str(event["message"]),
            span_id=str(event.get("span_id") or "") or None,
            parent_span_id=str(event.get("parent_span_id") or "") or None,
            duration_ms=event.get("duration_ms") if isinstance(event.get("duration_ms"), int) else None,
            provider=str(event.get("provider") or "") or None,
            model=str(event.get("model") or "") or None,
            retry_count=int(event.get("retry_count") or 0),
            reasoning_summary=str(event.get("reasoning_summary") or "") or None,
            input_summary=event.get("input_summary") if isinstance(event.get("input_summary"), dict) else {},
            output_summary=event.get("output_summary") if isinstance(event.get("output_summary"), dict) else {},
            warning_codes=[
                str(code)
                for code in (event.get("warning_codes") or [])
                if code
            ],
            error_source=event.get("error_source") if isinstance(event.get("error_source"), str) else None,
            token_usage=event.get("token_usage") if isinstance(event.get("token_usage"), dict) else {},
            errors=[str(error) for error in (event.get("errors") or []) if error],
            details=event.get("metadata", {}).get("details", {}) if isinstance(event.get("metadata"), dict) else {},
            metadata=event.get("metadata") if isinstance(event.get("metadata"), dict) else {},
            started_at=str(event.get("started_at") or "") or None,
            ended_at=str(event.get("ended_at") or "") or None,
            schema_version=str(event.get("schema_version") or "") or None,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write trace event")


async def _drain_trace_queue(queue: asyncio.Queue[dict[str, object]]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


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
    app.state.provider_config_report = settings.provider_readiness_report()
    app.state.runtime_readiness_report = await _runtime_readiness_report()
    if app.state.provider_config_report["status"] == "ok":
        logger.info("Provider configuration readiness passed.")
    else:
        logger.warning(
            "Provider configuration readiness failed: %s",
            json.dumps(app.state.provider_config_report["issues"], separators=(",", ":")),
        )
    if app.state.runtime_readiness_report["status"] == "ok":
        logger.info("Runtime dependency readiness passed.")
    else:
        logger.warning(
            "Runtime dependency readiness failed: %s",
            json.dumps(app.state.runtime_readiness_report, separators=(",", ":")),
        )
    _enforce_startup_readiness(
        app.state.provider_config_report,
        app.state.runtime_readiness_report,
    )
    try:
        pool = await db.create_pool()
        await db.bootstrap(pool)
        await ingest.ensure_hnsw_index(pool)
        app.state.pool = pool
        app.state.observability_pipeline = AsyncObservabilityPipeline(
            pool=pool,
            persist_event=_persist_trace_event,
            queue_size=settings.observability_queue_size,
            batch_size=settings.observability_batch_size,
            flush_interval_seconds=settings.observability_flush_interval_seconds,
        )
        await app.state.observability_pipeline.start()
        logger.info(
            "Service ready (embedding_dim=%d, top_k=%d)",
            settings.embedding_dimension,
            settings.top_k,
        )
    except Exception as exc:  # noqa: BLE001
        app.state.pool = None
        app.state.observability_pipeline = None
        logger.error(
            "Database unavailable at startup (%s: %s). "
            "Endpoints will return 503 until the DB is reachable. "
            "Check DATABASE_URL and Tailscale connectivity.",
            type(exc).__name__,
            exc,
        )
    yield
    # Shutdown
    observability_pipeline = getattr(app.state, "observability_pipeline", None)
    if observability_pipeline is not None:
        await observability_pipeline.stop()
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
setup_tracing(app, settings)
install_request_middleware(app)


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


@app.get("/metrics/prometheus", tags=["ops"])
async def prometheus_metrics_endpoint() -> Response:
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")


@app.get("/logs/days", tags=["ops"])
async def list_log_days_endpoint() -> dict[str, object]:
    """List available repo-local daily log files."""
    return {
        "log_dir": str(_observability_log_dir()),
        "results": _list_log_files(),
    }


@app.get("/logs/recent", tags=["ops"])
async def recent_logs_endpoint(
    day: str = "current",
    lines: int = 200,
) -> dict[str, object]:
    """Return the most recent lines from the selected repo-local log file."""
    resolved_day, path = _resolve_log_file(day)
    recent_lines = _tail_log_lines(path, lines=lines)
    return {
        "day": resolved_day,
        "file": path.name,
        "path": str(path),
        "lines": recent_lines,
        "total_lines_returned": len(recent_lines),
    }


@app.get("/logs/stream", tags=["ops"])
async def stream_logs_endpoint(
    day: str = "current",
    backlog: int = 100,
    follow: bool = True,
    poll_interval_ms: int = 1000,
) -> StreamingResponse:
    """Stream repo-local log lines as NDJSON."""
    resolved_day, path = _resolve_log_file(day)
    return StreamingResponse(
        _stream_log_lines(
            path=path,
            day=resolved_day,
            backlog=backlog,
            follow=follow and resolved_day == "current",
            poll_interval_ms=poll_interval_ms,
        ),
        media_type="application/x-ndjson",
    )


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


def _teach_alerts_from_stats(stats: dict[str, object]) -> tuple[str, list[dict[str, object]]]:
    active_count = int(stats.get("pending_active_count") or 0)
    expired_count = int(stats.get("pending_expired_count") or 0)
    alerts: list[dict[str, object]] = []

    if expired_count >= settings.teach_pending_expired_warn_threshold:
        alerts.append(
            {
                "code": "TEACH_PENDING_EXPIRED",
                "severity": "warning",
                "message": (
                    f"{expired_count} expired teach confirmation token(s) are waiting for cleanup."
                ),
                "value": expired_count,
                "threshold": settings.teach_pending_expired_warn_threshold,
            }
        )

    if active_count >= settings.teach_pending_active_warn_threshold:
        alerts.append(
            {
                "code": "TEACH_PENDING_BACKLOG",
                "severity": "warning",
                "message": (
                    f"{active_count} active teach confirmation token(s) exceed the backlog threshold."
                ),
                "value": active_count,
                "threshold": settings.teach_pending_active_warn_threshold,
            }
        )

    status = "warning" if alerts else "ok"
    return status, alerts


def _merge_health_status(current: str, candidate: str) -> str:
    order = {"ok": 0, "warning": 1, "error": 2}
    return candidate if order.get(candidate, 0) > order.get(current, 0) else current


def _routing_section(
    *,
    provider: str | None,
    model: str | None,
    api_key: str | None,
    base_url: str | None,
    fallback_provider: str | None = None,
    fallback_model: str | None = None,
    fallback_api_key: str | None = None,
    fallback_base_url: str | None = None,
) -> dict[str, object]:
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key_configured": bool((api_key or "").strip()),
        "fallback_provider": fallback_provider,
        "fallback_model": fallback_model,
        "fallback_base_url": fallback_base_url,
        "fallback_api_key_configured": bool((fallback_api_key or "").strip()),
    }


def _answer_model_section() -> dict[str, object]:
    return _routing_section(
        provider=settings.answer_model_provider or settings.reasoning_model_provider or settings.llm_provider,
        model=settings.answer_model or settings.reasoning_model or settings.llm_model,
        api_key=settings.answer_model_api_key or settings.reasoning_model_api_key or settings.llm_api_key,
        base_url=settings.answer_model_base_url or settings.reasoning_model_base_url or settings.llm_base_url,
        fallback_provider=settings.answer_fallback_provider or settings.llm_fallback_provider,
        fallback_model=settings.answer_fallback_model or settings.llm_fallback_model,
        fallback_api_key=settings.answer_fallback_api_key or settings.llm_fallback_api_key,
        fallback_base_url=settings.answer_fallback_base_url or settings.llm_fallback_base_url,
    )


def _model_routing_snapshot() -> ModelRoutingSnapshot:
    return ModelRoutingSnapshot(
        llm=_routing_section(
            provider=settings.llm_provider,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            fallback_provider=settings.llm_fallback_provider,
            fallback_model=settings.llm_fallback_model,
            fallback_api_key=settings.llm_fallback_api_key,
            fallback_base_url=settings.llm_fallback_base_url,
        ),
        sql=_routing_section(
            provider=settings.sql_model_provider or settings.llm_provider,
            model=settings.sql_model or settings.llm_model,
            api_key=settings.sql_model_api_key or settings.llm_api_key,
            base_url=settings.sql_model_base_url or settings.llm_base_url,
            fallback_provider=settings.sql_fallback_provider or settings.llm_fallback_provider,
            fallback_model=settings.sql_fallback_model or settings.llm_fallback_model,
            fallback_api_key=settings.sql_fallback_api_key or settings.llm_fallback_api_key,
            fallback_base_url=settings.sql_fallback_base_url or settings.llm_fallback_base_url,
        ),
        reasoning=_routing_section(
            provider=settings.reasoning_model_provider or settings.llm_provider,
            model=settings.reasoning_model,
            api_key=settings.reasoning_model_api_key or settings.llm_api_key,
            base_url=settings.reasoning_model_base_url or settings.llm_base_url,
            fallback_provider=settings.reasoning_fallback_provider or settings.llm_fallback_provider,
            fallback_model=settings.reasoning_fallback_model or settings.llm_fallback_model,
            fallback_api_key=settings.reasoning_fallback_api_key or settings.llm_fallback_api_key,
            fallback_base_url=settings.reasoning_fallback_base_url or settings.llm_fallback_base_url,
        ),
        query_rewrite=_routing_section(
            provider=settings.query_rewrite_model_provider or settings.llm_provider,
            model=settings.effective_query_rewrite_model,
            api_key=settings.query_rewrite_model_api_key or settings.llm_api_key,
            base_url=settings.query_rewrite_model_base_url or settings.llm_base_url,
            fallback_provider=settings.query_rewrite_fallback_provider or settings.llm_fallback_provider,
            fallback_model=settings.query_rewrite_fallback_model or settings.llm_fallback_model,
            fallback_api_key=settings.query_rewrite_fallback_api_key or settings.llm_fallback_api_key,
            fallback_base_url=settings.query_rewrite_fallback_base_url or settings.llm_fallback_base_url,
        ),
        answer=_answer_model_section(),
        embedding=_routing_section(
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url or settings.embedding_api_url,
        ),
        startup_enforcement_mode=settings.startup_enforcement_mode,
        provider_readiness=settings.provider_readiness_report(),
    )


def _apply_model_routing_patch(patch: ModelRoutingPatchRequest) -> ModelRoutingSnapshot:
    updates = patch.model_dump(exclude_unset=True)
    if not updates:
        return _model_routing_snapshot()

    snapshot: dict[str, object] = {}
    for key, value in updates.items():
        snapshot[key] = getattr(settings, key, None)
        setattr(settings, key, value)

    if "startup_enforcement_mode" in updates:
        mode = str(settings.startup_enforcement_mode).strip().lower()
        if mode not in {"warn", "strict"}:
            for key, value in snapshot.items():
                setattr(settings, key, value)
            raise HTTPException(
                status_code=422,
                detail="STARTUP_ENFORCEMENT_MODE must be one of: warn, strict",
            )
        settings.startup_enforcement_mode = mode

    report = settings.provider_readiness_report()
    if report["issues"]:
        for key, value in snapshot.items():
            setattr(settings, key, value)
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Invalid model routing configuration",
                "issues": report["issues"],
            },
        )

    return _model_routing_snapshot()


def _ask_model_snapshot() -> AskModelSnapshot:
    return AskModelSnapshot(**_answer_model_section())


def _apply_ask_model_patch(patch: AskModelPatchRequest) -> AskModelSnapshot:
    updates = patch.model_dump(exclude_unset=True)
    if not updates:
        return _ask_model_snapshot()

    field_map = {
        "provider": "answer_model_provider",
        "model": "answer_model",
        "api_key": "answer_model_api_key",
        "base_url": "answer_model_base_url",
        "fallback_provider": "answer_fallback_provider",
        "fallback_model": "answer_fallback_model",
        "fallback_api_key": "answer_fallback_api_key",
        "fallback_base_url": "answer_fallback_base_url",
    }
    snapshot: dict[str, object] = {}
    for key, value in updates.items():
        settings_key = field_map[key]
        snapshot[settings_key] = getattr(settings, settings_key, None)
        setattr(settings, settings_key, value)

    report = settings.provider_readiness_report()
    if report["issues"]:
        for key, value in snapshot.items():
            setattr(settings, key, value)
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Invalid ask model configuration",
                "issues": report["issues"],
            },
        )

    return _ask_model_snapshot()


def _env_role_summary(role: str) -> dict[str, object]:
    if role == LLMRole.SQL.value:
        return {
            "provider": settings.sql_model_provider or settings.llm_provider,
            "model": settings.sql_model or settings.llm_model,
        }
    if role == LLMRole.REASONING.value:
        return {
            "provider": settings.reasoning_model_provider or settings.llm_provider,
            "model": settings.reasoning_model,
        }
    if role == LLMRole.QUERY_REWRITE.value:
        return {
            "provider": settings.query_rewrite_model_provider or settings.llm_provider,
            "model": settings.effective_query_rewrite_model,
        }
    if role == LLMRole.ANSWER.value:
        answer_section = _answer_model_section()
        return {
            "provider": answer_section["provider"],
            "model": answer_section["model"],
        }
    if role == LLMRole.EMBEDDING.value:
        return {
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
        }
    raise ValueError(f"Unsupported role: {role}")


def _trace_recorder_for_request(request: Request, request_id: str | None = None) -> TraceRecorder:
    resolved_request_id = _resolve_request_id(request_id)
    return TraceRecorder(
        pool=request.app.state.pool,
        request_id=resolved_request_id,
        pipeline=getattr(request.app.state, "observability_pipeline", None),
    )


def _apply_live_role_routing(
    *,
    role: str,
    provider_name: str,
    model_name: str,
    base_url: str | None,
    api_key: str | None,
) -> None:
    provider_field = "llm_provider" if role == "general" else f"{role}_model_provider"
    model_field = "llm_model" if role == "general" else f"{role}_model"
    base_url_field = "llm_base_url" if role == "general" else f"{role}_model_base_url"
    api_key_field = "llm_api_key" if role == "general" else f"{role}_model_api_key"
    setattr(settings, provider_field, provider_name)
    setattr(settings, model_field, model_name)
    setattr(settings, base_url_field, base_url)
    setattr(settings, api_key_field, api_key)


async def _select_provider_api_key(
    pool: asyncpg.Pool,
    provider_id,
) -> str | None:
    key_records = await provider_store.list_api_keys(pool, provider_id)
    active_key = next((record for record in key_records if record.is_active), None)
    if active_key is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT api_key_enc FROM nl2sql_llm_api_keys WHERE id = $1",
            active_key.id,
        )
    if row is None or not row.get("api_key_enc"):
        return None
    return decrypt_api_key(str(row["api_key_enc"]))


async def _runtime_readiness_report() -> dict[str, object]:
    mysql_target = await mysql_executor.mysql_target_readiness(settings)
    schema_assets = schema_loader.loader_readiness()
    status = _merge_health_status(
        str(mysql_target.get("status", "ok")),
        str(schema_assets.get("status", "ok")),
    )
    return {
        "status": status,
        "mysql_target": mysql_target,
        "schema_assets": schema_assets,
    }


def _startup_enforcement_errors(
    provider_config: dict[str, object],
    runtime_report: dict[str, object],
) -> list[str]:
    errors: list[str] = []
    if provider_config.get("status") != "ok":
        errors.append(
            f"provider config not ready ({len(provider_config.get('issues', []))} issue(s))"
        )
    if runtime_report.get("status") != "ok":
        mysql_target = runtime_report.get("mysql_target", {})
        schema_assets = runtime_report.get("schema_assets", {})
        if isinstance(mysql_target, dict) and mysql_target.get("status") != "ok":
            errors.append(
                f"MySQL target not ready ({len(mysql_target.get('issues', []))} issue(s))"
            )
        if isinstance(schema_assets, dict) and schema_assets.get("status") != "ok":
            errors.append(
                f"schema assets not ready ({len(schema_assets.get('issues', []))} issue(s))"
            )
    return errors


def _enforce_startup_readiness(
    provider_config: dict[str, object],
    runtime_report: dict[str, object],
) -> None:
    if settings.startup_enforcement_mode != "strict":
        return
    errors = _startup_enforcement_errors(provider_config, runtime_report)
    if errors:
        raise RuntimeError(
            "Startup readiness enforcement failed in strict mode: " + "; ".join(errors)
        )


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
    health_status = "ok"
    provider_config = getattr(request.app.state, "provider_config_report", settings.provider_readiness_report())
    if provider_config.get("status") != "ok":
        health_status = "error"
    mysql_target = await mysql_executor.mysql_target_readiness(settings)
    health_status = _merge_health_status(health_status, str(mysql_target.get("status", "ok")))
    schema_assets = schema_loader.loader_readiness()
    health_status = _merge_health_status(health_status, str(schema_assets.get("status", "ok")))
    teach_status = "unavailable"
    teach_alerts: list[dict[str, object]] = []
    pool = request.app.state.pool
    acquire = getattr(pool, "acquire", None)
    if pool is not None and acquire is not None and not inspect.iscoroutinefunction(acquire):
        try:
            teach_stats = await db.get_pending_teach_confirmation_stats(pool)
            teach_status, teach_alerts = _teach_alerts_from_stats(teach_stats)
            if teach_status == "warning":
                health_status = _merge_health_status(health_status, "warning")
        except Exception:
            teach_status = "unavailable"
    return {
        "status": health_status,
        "db": db_status,
        "provider_config": {
            "status": provider_config.get("status", "error"),
            "issue_count": len(provider_config.get("issues", [])),
        },
        "mysql_target": {
            "status": mysql_target.get("status", "error"),
            "issue_count": len(mysql_target.get("issues", [])),
        },
        "schema_assets": {
            "status": schema_assets.get("status", "error"),
            "issue_count": len(schema_assets.get("issues", [])),
        },
        "teach_confirmations": {
            "status": teach_status,
            "alerts": teach_alerts,
        },
    }




@app.get("/health/llm", tags=["ops"])
async def health_llm(role: str = LLMRole.SQL.value) -> dict[str, object]:
    role = role.strip()
    allowed_roles = set(ALL_ROLES)
    if role not in allowed_roles:
        raise HTTPException(status_code=422, detail=f"Unsupported LLM health role: {role}")

    model = {
        LLMRole.SQL.value: settings.sql_model or settings.llm_model,
        LLMRole.REASONING.value: settings.reasoning_model,
        LLMRole.QUERY_REWRITE.value: settings.effective_query_rewrite_model,
        LLMRole.ANSWER.value: settings.answer_model or settings.reasoning_model,
        LLMRole.DEFAULT.value: settings.llm_model,
        LLMRole.EMBEDDING.value: settings.embedding_model,
    }[role]
    if role == LLMRole.EMBEDDING.value:
        result = await embed.health_probe()
    else:
        timeout = min(settings.llm_timeout, 10)
        provider = LLMFactory.create_for_role(
            settings,
            role=role,
            model=model,
            default_timeout=timeout,
        )
        try:
            result = await provider.health()
        except Exception as exc:  # noqa: BLE001
            result = {
                "provider": provider.provider_name,
                "model": provider.model_name,
                "status": "error",
                "healthy": False,
                "latency_ms": None,
                "last_probe_latency_ms": None,
                "message": str(exc),
                "error_message": str(exc),
                "error_type": exc.__class__.__name__,
            }
    result.setdefault("role", role)
    result.setdefault("provider", None)
    result.setdefault("model", model)
    result.setdefault("healthy", result.get("status") == "ok")
    if result.get("last_probe_latency_ms") is None and result.get("latency_ms") is not None:
        result["last_probe_latency_ms"] = result.get("latency_ms")
    provider_config = settings.provider_readiness_report()
    return {
        "role": role,
        "provider_config": {
            "status": provider_config["status"],
            "issues": [
                issue
                for issue in provider_config["issues"]
                if str(issue.get("target", "")).startswith(role.upper())
                or issue.get("target") == "LLM_PROVIDER"
            ],
        },
        **result,
    }


@app.get("/health/config", tags=["ops"])
async def health_config() -> dict[str, object]:
    return settings.provider_readiness_report()


@app.get("/health/runtime", tags=["ops"])
async def health_runtime() -> dict[str, object]:
    return await _runtime_readiness_report()


@app.get("/config/model-routing", tags=["ops"])
async def get_model_routing() -> ModelRoutingSnapshot:
    return _model_routing_snapshot()


@app.patch("/config/model-routing", tags=["ops"])
async def patch_model_routing(body: ModelRoutingPatchRequest) -> ModelRoutingSnapshot:
    return _apply_model_routing_patch(body)


@app.get("/config/ask-model", tags=["ops"])
async def get_ask_model() -> AskModelSnapshot:
    return _ask_model_snapshot()


@app.patch("/config/ask-model", tags=["ops"])
async def patch_ask_model(body: AskModelPatchRequest) -> AskModelSnapshot:
    return _apply_ask_model_patch(body)


@app.patch("/config/active-model/{role}", tags=["ops"])
async def patch_active_model(
    role: str,
    body: ActiveModelPatchRequest,
    request: Request,
) -> dict[str, object]:
    pool = await _require_pool(request)
    trace_recorder = _trace_recorder_for_request(request)
    await trace_recorder.emit(
        stage="provider_management",
        status="started",
        message="Updating active model for role.",
        details={"role": role, "model_id": str(body.model_id)},
    )
    model = await provider_store.get_model_by_id(pool, body.model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found.")
    if model.role != role:
        raise HTTPException(status_code=422, detail="Model role does not match requested role.")
    default_model = await provider_store.set_default_model(pool, body.model_id, role)
    provider = await provider_store.get_provider(pool, default_model.provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found.")
    api_key = await _select_provider_api_key(pool, provider.id)
    _apply_live_role_routing(
        role=role,
        provider_name=provider.provider_name,
        model_name=default_model.model_name,
        base_url=provider.base_url,
        api_key=api_key,
    )
    await trace_recorder.emit(
        stage="provider_management",
        status="completed",
        message="Active model updated for role.",
        details={"role": role, "provider_name": provider.provider_name, "model_name": default_model.model_name},
    )
    return {
        "role": role,
        "default_model": default_model.model_dump(mode="json"),
        "live_routing": _model_routing_snapshot().model_dump(mode="json"),
    }


@app.get("/providers", response_model=list[ProviderConfigResponse], tags=["ops"])
async def providers_list(request: Request) -> list[ProviderConfigResponse]:
    pool = await _require_pool(request)
    trace_recorder = _trace_recorder_for_request(request)
    await trace_recorder.emit(stage="provider_management", status="started", message="Listing providers.")
    providers = await provider_store.list_providers(pool)
    await trace_recorder.emit(
        stage="provider_management",
        status="completed",
        message="Listed providers.",
        details={"provider_count": len(providers)},
    )
    return providers


@app.post("/providers", response_model=ProviderConfigResponse, tags=["ops"])
async def providers_create(body: CreateProviderRequest, request: Request) -> ProviderConfigResponse:
    pool = await _require_pool(request)
    trace_recorder = _trace_recorder_for_request(request)
    await trace_recorder.emit(
        stage="provider_management",
        status="started",
        message="Creating provider.",
        details={"provider_name": body.provider_name},
    )
    provider = await provider_store.create_provider(pool, body)
    await trace_recorder.emit(
        stage="provider_management",
        status="completed",
        message="Provider created.",
        details={"provider_id": str(provider.id)},
    )
    return provider


@app.get("/providers/{provider_id}", response_model=ProviderConfigResponse, tags=["ops"])
async def providers_get(provider_id: uuid.UUID, request: Request) -> ProviderConfigResponse:
    pool = await _require_pool(request)
    provider = await provider_store.get_provider(pool, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found.")
    return provider


@app.patch("/providers/{provider_id}", response_model=ProviderConfigResponse, tags=["ops"])
async def providers_patch(
    provider_id: uuid.UUID,
    body: UpdateProviderRequest,
    request: Request,
) -> ProviderConfigResponse:
    pool = await _require_pool(request)
    try:
        return await provider_store.update_provider(pool, provider_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/providers/{provider_id}", tags=["ops"])
async def providers_delete(provider_id: uuid.UUID, request: Request) -> dict[str, object]:
    pool = await _require_pool(request)
    deleted = await provider_store.deactivate_provider(pool, provider_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider not found.")
    return {"ok": True, "provider_id": str(provider_id)}


@app.post("/providers/{provider_id}/test", response_model=ProviderTestResult, tags=["ops"])
async def provider_test(provider_id: uuid.UUID, request: Request) -> ProviderTestResult:
    pool = await _require_pool(request)
    provider = await provider_store.get_provider(pool, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found.")
    api_key = await _select_provider_api_key(pool, provider_id) or ""
    models = await provider_store.list_models(pool, active_only=True)
    model_name = next((item.model_name for item in models if item.provider_id == provider_id), "")
    return await test_provider_connection(provider.model_dump(mode="json"), api_key, model_name)


@app.get("/providers/{provider_id}/models", tags=["ops"])
async def provider_models(provider_id: uuid.UUID, request: Request) -> dict[str, object]:
    pool = await _require_pool(request)
    provider = await provider_store.get_provider(pool, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found.")
    api_key = await _select_provider_api_key(pool, provider_id) or ""
    probe = await list_provider_models(provider.model_dump(mode="json"), api_key)
    return probe.model_dump(mode="json")


@app.post("/providers/{provider_id}/keys", response_model=ApiKeyRecord, tags=["ops"])
async def provider_add_key(
    provider_id: uuid.UUID,
    body: AddApiKeyRequest,
    request: Request,
) -> ApiKeyRecord:
    if not is_key_vault_configured():
        raise HTTPException(status_code=503, detail="PROVIDER_KEY_ENCRYPTION_SECRET is not configured.")
    pool = await _require_pool(request)
    try:
        return await provider_store.add_api_key(pool, provider_id, body.key_label, body.api_key)
    except KeyVaultUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/providers/{provider_id}/keys", response_model=list[ApiKeyRecord], tags=["ops"])
async def provider_keys(provider_id: uuid.UUID, request: Request) -> list[ApiKeyRecord]:
    pool = await _require_pool(request)
    return await provider_store.list_api_keys(pool, provider_id)


@app.delete("/providers/{provider_id}/keys/{key_id}", tags=["ops"])
async def provider_delete_key(
    provider_id: uuid.UUID,
    key_id: uuid.UUID,
    request: Request,
) -> dict[str, object]:
    del provider_id
    pool = await _require_pool(request)
    deleted = await provider_store.deactivate_api_key(pool, key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found.")
    return {"ok": True, "key_id": str(key_id)}


@app.get("/model-registry", response_model=list[ModelRecord], tags=["ops"])
async def model_registry_list(
    request: Request,
    role: str | None = None,
    active_only: bool = True,
) -> list[ModelRecord]:
    pool = await _require_pool(request)
    return await provider_store.list_models(pool, role=role, active_only=active_only)


@app.post("/model-registry", response_model=ModelRecord, tags=["ops"])
async def model_registry_create(
    body: RegisterModelRequest,
    request: Request,
) -> ModelRecord:
    pool = await _require_pool(request)
    return await provider_store.register_model(pool, body)


@app.patch("/model-registry/{model_id}", response_model=ModelRecord, tags=["ops"])
async def model_registry_patch(
    model_id: uuid.UUID,
    body: UpdateModelRequest,
    request: Request,
) -> ModelRecord:
    pool = await _require_pool(request)
    try:
        return await provider_store.update_model(pool, model_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/model-registry/{model_id}", tags=["ops"])
async def model_registry_delete(model_id: uuid.UUID, request: Request) -> dict[str, object]:
    pool = await _require_pool(request)
    deleted = await provider_store.deactivate_model(pool, model_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Model not found.")
    return {"ok": True, "model_id": str(model_id)}


@app.post("/model-registry/{model_id}/set-default", response_model=ModelRecord, tags=["ops"])
async def model_registry_set_default(model_id: uuid.UUID, request: Request) -> ModelRecord:
    pool = await _require_pool(request)
    existing = await provider_store.get_model_by_id(pool, model_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Model not found.")
    return await provider_store.set_default_model(pool, model_id, existing.role)


@app.get("/model-registry/default", tags=["ops"])
async def model_registry_defaults(request: Request) -> dict[str, object]:
    pool = await _require_pool(request)
    defaults: dict[str, object] = {}
    default_roles = [
        "general",
        *(generation_role.value for generation_role in GENERATION_ROLES),
        LLMRole.EMBEDDING.value,
    ]
    for role in default_roles:
        model = await provider_store.get_default_model(pool, role)
        defaults[role] = model.model_dump(mode="json") if model is not None else None
    return defaults


@app.get("/model-registry/active-summary", tags=["ops"])
async def model_registry_active_summary(request: Request) -> dict[str, object]:
    pool = await _require_pool(request)
    roles: dict[str, object] = {}
    summary_roles = [
        *(generation_role.value for generation_role in GENERATION_ROLES),
        LLMRole.EMBEDDING.value,
    ]
    for role in summary_roles:
        model = await provider_store.get_default_model(pool, role)
        if model is not None:
            roles[role] = {
                "provider": model.provider_name,
                "model": model.model_name,
                "source": "db_registry",
                "model_id": str(model.id),
            }
            continue
        env_summary = _env_role_summary(role)
        roles[role] = {
            "provider": env_summary["provider"],
            "model": env_summary["model"],
            "source": "env_config",
            "model_id": None,
        }
    return {"roles": roles}


@app.get("/health/vector", tags=["ops"])
async def health_vector(request: Request) -> dict[str, object]:
    provider_config = settings.provider_readiness_report()
    if request.app.state.pool is None:
        await _try_reconnect_pool(request)
    if request.app.state.pool is None:
        return {
            "status": "warning" if provider_config["status"] == "ok" else "error",
            "vector_db": settings.vector_provider,
            "db": "unavailable",
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
            "provider_config": provider_config,
        }
    try:
        await asyncio.wait_for(request.app.state.pool.execute("SELECT 1"), timeout=3)
        return {
            "status": "ok" if provider_config["status"] == "ok" else "error",
            "vector_db": settings.vector_provider,
            "db": "connected",
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "embedding_dimension": settings.embedding_dimension,
            "provider_config": provider_config,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "warning" if provider_config["status"] == "ok" else "error",
            "vector_db": settings.vector_provider,
            "db": "unreachable",
            "error": str(exc),
            "provider_config": provider_config,
        }


@app.get("/metrics/llm", tags=["ops"])
async def llm_metrics_endpoint() -> dict[str, object]:
    return {"results": llm_metrics.snapshot()}


@app.get("/metrics/teach", tags=["ops"])
async def teach_metrics_endpoint(request: Request) -> dict[str, object]:
    pool = await _require_pool(request)
    stats = await db.get_pending_teach_confirmation_stats(pool)
    status, alerts = _teach_alerts_from_stats(stats)
    return {
        **stats,
        "status": status,
        "alerts": alerts,
        "thresholds": {
            "pending_active_warn_threshold": settings.teach_pending_active_warn_threshold,
            "pending_expired_warn_threshold": settings.teach_pending_expired_warn_threshold,
        },
    }


@app.get("/telemetry/recent", tags=["ops"])
async def telemetry_recent_endpoint(
    request: Request,
    limit: int = settings.telemetry_recent_limit_default,
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


@app.get("/telemetry/trace/{request_id}", tags=["ops"])
async def telemetry_trace_endpoint(
    request: Request,
    request_id: str,
    limit: int = settings.telemetry_trace_limit_default,
) -> dict[str, object]:
    """Return ordered per-stage trace events for one request id."""
    pool = await _require_pool(request)
    results = await db.list_trace_events(pool, request_id=request_id, limit=limit)
    return {"request_id": request_id, "results": results, "total": len(results)}


@app.get("/failures", tags=["ops"])
async def list_failures_endpoint(
    request: Request,
    limit: int = 100,
    endpoint: str | None = None,
) -> list[dict[str, object]]:
    """Return recent failed requests with pre-built teach suggestions for review."""
    pool = await _require_pool(request)
    return await db.list_failure_logs(pool, limit=limit, endpoint=endpoint)


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


@app.get("/teach/pending", tags=["learning"])
async def list_pending_teach_confirmations_endpoint(
    request: Request,
    limit: int = 100,
    include_expired: bool = False,
) -> dict[str, object]:
    pool = await _require_pool(request)
    results = await db.list_pending_teach_confirmations(
        pool,
        limit=limit,
        include_expired=include_expired,
    )
    stats = await db.get_pending_teach_confirmation_stats(pool)
    return {"results": results, "stats": stats}


@app.post("/teach/pending/cleanup", tags=["learning"])
async def cleanup_pending_teach_confirmations_endpoint(
    request: Request,
) -> dict[str, object]:
    pool = await _require_pool(request)
    deleted = await db.cleanup_pending_teach_confirmations(pool)
    stats = await db.get_pending_teach_confirmation_stats(pool)
    return {"deleted": deleted, "stats": stats}


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
    _bind_request_context(request_id=request_id, endpoint="/generate-sql")
    started = time.monotonic()
    trace_recorder = TraceRecorder(
        pool=pool,
        request_id=request_id,
        pipeline=getattr(http_request.app.state, "observability_pipeline", None),
    )
    set_current_trace_recorder(trace_recorder)
    await trace_recorder.emit(
        stage="request_received",
        status="started",
        message="Received SQL preview request.",
        details={"endpoint": "/generate-sql", "top_k": top_k},
        input_summary={"query_preview": summarize_text(request.query), "top_k": top_k},
    )
    result = await generate_sql(
        request.query,
        pool,
        settings,
        top_k,
        trace_callback=trace_recorder.emit,
    )
    result = _attach_review_prompt(result, request.query)
    result = _enrich_response_with_context(result)
    await trace_recorder.emit(
        stage="complete",
        status=result.status,
        message="SQL preview request completed.",
        duration_ms=_elapsed_ms(started),
        warning_codes=[warning.code.value for warning in getattr(result, "warnings", [])],
        error_source=_derive_error_source(getattr(result, "warnings", [])),
        output_summary=sanitize_value(result.model_dump(mode="json")) or {},
        metadata=_generation_metadata(result),
    )
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
    _bind_request_context(request_id=request_id, endpoint="/ask")
    started = time.monotonic()
    trace_recorder = TraceRecorder(
        pool=pool,
        request_id=request_id,
        pipeline=getattr(http_request.app.state, "observability_pipeline", None),
    )
    set_current_trace_recorder(trace_recorder)
    await trace_recorder.emit(
        stage="request_received",
        status="started",
        message="Received ask request.",
        details={"endpoint": "/ask", "top_k": top_k},
        input_summary={"query_preview": summarize_text(request.query), "top_k": top_k},
    )
    cache_epoch: int | None = None
    query_embedding: list[float] | None = None
    deterministic_candidate = False

    # --- Ask cache: exact match -------------------------------------------------
    if settings.ask_cache_enabled:
        cached_ask = ask_cache.get_exact(request.query, top_k)
        if cached_ask:
            cached_ask.pop("_top_k", None)
            cached_ask["cache_hit"] = True
            cached_ask["cache_source"] = CacheSource.MEMORY_EXACT.value
            response = _ask_success_from_cache(cached_ask)
            await trace_recorder.emit(
                stage="cache_lookup",
                status="completed",
                message="Ask cache hit in memory.",
                details={"cache_source": CacheSource.MEMORY_EXACT.value},
            )
            await trace_recorder.emit(
                stage="complete",
                status=response.status,
                message="Ask request completed from cache.",
                duration_ms=_elapsed_ms(started),
            )
            return _enrich_response_with_context(response)

    try:
        query_embedding = await _load_query_embedding(request.query)
    except Exception:
        logger.exception("Failed to load ask cache embedding")
        query_embedding = None
    if query_embedding is not None:
        deterministic_tables_in_scope: list[str] = []
        deterministic_matched_groups: list[str] = []
        try:
            search_query = await query_rewriter.rewrite_search_query(request.query, pool, settings)
            deterministic_retrieved = await retrieve.retrieve_groups(
                query=request.query,
                top_k=top_k,
                pool=pool,
                search_query=search_query,
            )
            deterministic_tables_in_scope = list(
                _result_value(deterministic_retrieved, "tables_in_scope") or []
            )
            deterministic_matched_groups = list(
                _result_value(deterministic_retrieved, "matched_groups") or []
            )
        except Exception:
            logger.exception("Failed to load deterministic schema context")
        deterministic_candidate = await is_deterministic_generation_candidate(
            query=request.query,
            query_embedding=query_embedding,
            settings=settings,
            vector_store=PgVectorStore(pool),
            known_schema_tables=deterministic_tables_in_scope,
            matched_groups=deterministic_matched_groups,
        )

    # --- Ask cache: semantic match -----------------------------------------------
    if settings.ask_cache_enabled and not deterministic_candidate:
        try:
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
                response = _ask_success_from_cache(sem_ask)
                await trace_recorder.emit(
                    stage="cache_lookup",
                    status="completed",
                    message="Semantic ask cache hit in memory.",
                    details={"cache_source": CacheSource.MEMORY_SEMANTIC.value},
                )
                await trace_recorder.emit(
                    stage="complete",
                    status=response.status,
                    message="Ask request completed from cache.",
                    duration_ms=_elapsed_ms(started),
                )
                return _enrich_response_with_context(response)
        except Exception:
            pass  # semantic lookup is best-effort

    if settings.ask_cache_enabled:
        try:
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
                response = _ask_success_from_cache(cached_ask)
                await trace_recorder.emit(
                    stage="cache_lookup",
                    status="completed",
                    message="Ask cache hit in PostgreSQL.",
                    details={"cache_source": CacheSource.DB_EXACT.value},
                )
                await trace_recorder.emit(
                    stage="complete",
                    status=response.status,
                    message="Ask request completed from cache.",
                    duration_ms=_elapsed_ms(started),
                )
                return response

            if query_embedding is not None:
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
                    response = _ask_success_from_cache(sem_ask)
                    await trace_recorder.emit(
                        stage="cache_lookup",
                        status="completed",
                        message="Semantic ask cache hit in PostgreSQL.",
                        details={"cache_source": CacheSource.DB_SEMANTIC.value},
                    )
                    await trace_recorder.emit(
                        stage="complete",
                        status=response.status,
                        message="Ask request completed from cache.",
                        duration_ms=_elapsed_ms(started),
                    )
                    return _enrich_response_with_context(response)
        except Exception:
            logger.exception("Failed DB ask cache lookup")

    try:
        response = await asyncio.wait_for(
            _run_ask_workflow(
                request=request,
                pool=pool,
                top_k=top_k,
                request_id=request_id,
                started=started,
                cache_epoch=cache_epoch,
                query_embedding=query_embedding,
                trace_recorder=trace_recorder,
            ),
            timeout=settings.ask_timeout,
        )
        return _enrich_response_with_context(response)
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
        elapsed = _elapsed_ms(started)
        warning_codes_list = [warning.code.value for warning in response.warnings]
        error_src = _derive_error_source(response.warnings)
        asyncio.create_task(
            _log_request_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                top_k=top_k,
                status=response.status,
                attempt_count=response.attempt_count,
                latency_ms=elapsed,
                stage_latencies_ms={},
                warning_codes=warning_codes_list,
                error_source=error_src,
                metadata={"sql_present": False},
            )
        )
        asyncio.create_task(
            _log_failure_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                warning_codes=warning_codes_list,
                error_source=error_src,
                sql_preview=None,
                tables_attempted=[],
                latency_ms=elapsed,
                trace_emit=trace_recorder.emit,
            )
        )
        await trace_recorder.emit(
            stage="complete",
            status=response.status,
            message=response.warnings[0].message,
            duration_ms=elapsed,
            warning_codes=warning_codes_list,
            error_source=error_src,
        )
        return _enrich_response_with_context(response)


async def _run_ask_workflow(
    request: AskRequest,
    pool: asyncpg.Pool,
    top_k: int,
    request_id: str,
    started: float,
    cache_epoch: int | None = None,
    query_embedding: list[float] | None = None,
    trace_recorder: TraceRecorder | None = None,
) -> AskResponse:
    stage_latencies_ms: dict[str, int] = {}

    sql_started = time.monotonic()
    if trace_recorder is not None:
        await trace_recorder.emit(
            stage="sql_generation",
            status="started",
            message="Generating SQL for ask workflow.",
        )
    sql_result = await generate_sql(
        request.query,
        pool,
        settings,
        top_k,
        trace_callback=trace_recorder.emit if trace_recorder is not None else None,
    )
    stage_latencies_ms["sql_generation"] = _elapsed_ms(sql_started)
    _merged_sql_latencies = {**(sql_result.stage_latencies_ms or {}), **stage_latencies_ms}
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
        response = sql_result.model_copy(update={"stage_latencies_ms": _merged_sql_latencies})
        if trace_recorder is not None:
            await trace_recorder.emit(
                stage="complete",
                status=response.status,
                message=response.failure_reason,
                duration_ms=_elapsed_ms(started),
                error_source="clarification",
                details={
                    "suggestion_count": len(response.suggestions),
                    "stage_latencies_ms": _merged_sql_latencies,
                },
            )
        return _enrich_response_with_context(response)
    if sql_result.status == "rejected":
        response = AskRejected(
            sql=None,
            warnings=sql_result.warnings,
            attempt_count=sql_result.attempt_count,
            cache_hit=False,
            cache_source=CacheSource.NONE,
            react_trace=sql_result.react_trace,
            stage_latencies_ms=_merged_sql_latencies,
        )
        elapsed = _elapsed_ms(started)
        warning_codes_list = [warning.code.value for warning in response.warnings]
        error_src = _derive_error_source(response.warnings)
        asyncio.create_task(
            _log_request_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                top_k=top_k,
                status=response.status,
                attempt_count=response.attempt_count,
                latency_ms=elapsed,
                stage_latencies_ms=stage_latencies_ms,
                warning_codes=warning_codes_list,
                error_source=error_src,
                metadata={
                    "sql_present": response.sql is not None,
                    "tables_used": [],
                    "matched_groups": [],
                },
            )
        )
        asyncio.create_task(
            _log_failure_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                warning_codes=warning_codes_list,
                error_source=error_src,
                sql_preview=None,
                tables_attempted=[],
                latency_ms=elapsed,
                trace_emit=trace_recorder.emit if trace_recorder is not None else None,
            )
        )
        if trace_recorder is not None:
            await trace_recorder.emit(
                stage="complete",
                status=response.status,
                message="Ask request stopped during SQL generation.",
                duration_ms=elapsed,
                warning_codes=warning_codes_list,
                error_source=error_src,
                details={"stage_latencies_ms": _merged_sql_latencies},
            )
        return response

    capped_sql = mysql_executor.apply_row_cap(sql_result.sql, cap=50)
    execution_started = time.monotonic()
    if trace_recorder is not None:
        await trace_recorder.emit(
            stage="execution",
            status="started",
            message="Executing bounded SQL on MySQL.",
            details={
                "tables_used": sql_result.tables_used,
                "sql_preview": capped_sql[:500],
            },
        )
    columns, rows, execution_warnings = await mysql_executor.execute_sql(
        sql=capped_sql,
        settings=settings,
    )
    stage_latencies_ms["execution"] = _elapsed_ms(execution_started)
    if trace_recorder is not None:
        await trace_recorder.emit(
            stage="execution",
            status="completed" if not execution_warnings else "failed",
            message=(
                "SQL execution completed."
                if not execution_warnings
                else "SQL execution failed."
            ),
            duration_ms=stage_latencies_ms["execution"],
            warning_codes=[warning.code.value for warning in execution_warnings],
            error_source="execution" if execution_warnings else None,
            details={
                "row_count": len(rows),
                "columns": columns,
                "sql_preview": capped_sql[:500],
            },
        )
    if execution_warnings:
        response = AskRejected(
            sql=capped_sql,
            warnings=[*sql_result.warnings, *execution_warnings],
            attempt_count=sql_result.attempt_count,
            cache_hit=False,
            cache_source=CacheSource.NONE,
            react_trace=sql_result.react_trace,
            stage_latencies_ms={**_merged_sql_latencies, **stage_latencies_ms},
        )
        elapsed = _elapsed_ms(started)
        warning_codes_list = [warning.code.value for warning in response.warnings]
        error_src = _derive_error_source(response.warnings)
        asyncio.create_task(
            _log_request_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                top_k=top_k,
                status=response.status,
                attempt_count=response.attempt_count,
                latency_ms=elapsed,
                stage_latencies_ms=stage_latencies_ms,
                warning_codes=warning_codes_list,
                error_source=error_src,
                metadata={
                    "sql_present": response.sql is not None,
                    "tables_used": sql_result.tables_used,
                    "matched_groups": sql_result.matched_groups,
                },
            )
        )
        asyncio.create_task(
            _log_failure_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                warning_codes=warning_codes_list,
                error_source=error_src,
                sql_preview=capped_sql,
                tables_attempted=sql_result.tables_used,
                latency_ms=elapsed,
                trace_emit=trace_recorder.emit if trace_recorder is not None else None,
            )
        )
        if trace_recorder is not None:
            await trace_recorder.emit(
                stage="complete",
                status=response.status,
                message="Ask request stopped during SQL execution.",
                duration_ms=elapsed,
                warning_codes=warning_codes_list,
                error_source=error_src,
                details={"stage_latencies_ms": response.stage_latencies_ms or {}},
            )
        return response

    answer_started = time.monotonic()
    if trace_recorder is not None:
        await trace_recorder.emit(
            stage="answer_generation",
            status="started",
            message="Generating final answer from result rows.",
            details={"row_count": len(rows), "columns": columns},
        )
    if _should_use_deterministic_fallback(sql_result.matched_groups):
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
    if trace_recorder is not None:
        await trace_recorder.emit(
            stage="answer_generation",
            status="completed" if answer_text is not None else "failed",
            message=(
                "Answer generation completed."
                if answer_text is not None
                else "Answer generation failed."
            ),
            duration_ms=stage_latencies_ms["answer_generation"],
            warning_codes=[warning.code.value for warning in answer_warnings],
            error_source="answer_generation" if answer_text is None else None,
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
        response = AskRejected(
            sql=capped_sql,
            warnings=[*sql_result.warnings, *enriched_answer_warnings],
            attempt_count=sql_result.attempt_count,
            cache_hit=False,
            cache_source=CacheSource.NONE,
            react_trace=sql_result.react_trace,
            stage_latencies_ms={**_merged_sql_latencies, **stage_latencies_ms},
        )
        elapsed = _elapsed_ms(started)
        warning_codes_list = [warning.code.value for warning in response.warnings]
        error_src = _derive_error_source(response.warnings)
        asyncio.create_task(
            _log_request_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                top_k=top_k,
                status=response.status,
                attempt_count=response.attempt_count,
                latency_ms=elapsed,
                stage_latencies_ms=stage_latencies_ms,
                warning_codes=warning_codes_list,
                error_source=error_src,
                metadata={
                    "sql_present": response.sql is not None,
                    "row_count": len(rows),
                    "tables_used": sql_result.tables_used,
                    "matched_groups": sql_result.matched_groups,
                },
            )
        )
        asyncio.create_task(
            _log_failure_event(
                pool,
                request_id=request_id,
                endpoint="/ask",
                query_text=request.query,
                warning_codes=warning_codes_list,
                error_source=error_src,
                sql_preview=capped_sql,
                tables_attempted=sql_result.tables_used,
                latency_ms=elapsed,
                trace_emit=trace_recorder.emit if trace_recorder is not None else None,
            )
        )
        if trace_recorder is not None:
            await trace_recorder.emit(
                stage="complete",
                status=response.status,
                message="Ask request stopped during answer generation.",
                duration_ms=elapsed,
                warning_codes=warning_codes_list,
                error_source=error_src,
                details={"stage_latencies_ms": response.stage_latencies_ms or {}},
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
        stage_latencies_ms={**_merged_sql_latencies, **stage_latencies_ms},
        review_prompt=_build_sql_review_prompt(
            query=request.query,
            sql=capped_sql,
            tables_used=sql_result.tables_used,
        ),
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
    deterministic_response = any(
        group.startswith("deterministic_") for group in response.matched_groups
    )
    if settings.ask_cache_enabled:
        payload = response.model_dump(mode="json")
        payload["cache_hit"] = False
        payload["cache_source"] = CacheSource.NONE.value
        if query_embedding is None and not deterministic_response:
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
            if trace_recorder is not None:
                await trace_recorder.emit(
                    stage="cache_write",
                    status="completed",
                    message="Stored successful ask response in cache.",
                    details={"endpoint": "ask"},
                )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist ask cache entry")
            if trace_recorder is not None:
                await trace_recorder.emit(
                    stage="cache_write",
                    status="warning",
                    message="Failed to persist ask cache entry.",
                    error_source="cache_write",
                )
    if trace_recorder is not None:
        await trace_recorder.emit(
            stage="complete",
            status=response.status,
            message="Ask request completed.",
            duration_ms=_elapsed_ms(started),
            warning_codes=[warning.code.value for warning in response.warnings],
            error_source=_derive_error_source(response.warnings),
            details={
                "stage_latencies_ms": response.stage_latencies_ms or {},
                "row_count": response.row_count,
                "tables_used": response.tables_used,
                "matched_groups": response.matched_groups,
            },
        )
    return response


def _json_event(event_name: str, **payload: object) -> str:
    base_payload = {key: value for key, value in _context_metadata().items() if value}
    payload.pop("event", None)
    return json.dumps(
        {**base_payload, **payload, "event": event_name},
        default=str,
        separators=(",", ":"),
    ) + "\n"


def _warning_payload(warnings: list[SqlWarning]) -> list[dict]:
    return [warning.model_dump(mode="json") for warning in warnings]


def _remaining_timeout_seconds(started: float, timeout_seconds: float) -> float:
    return max(0.001, timeout_seconds - (time.monotonic() - started))


def _ask_stream_timeout_warning(stage: str, timeout_seconds: float) -> SqlWarning:
    return SqlWarning(
        code=WarningCode.REQUEST_TIMEOUT,
        message=(
            "Streaming ask exceeded the service time budget "
            f"of {timeout_seconds}s during {stage}."
        ),
    )


def _should_use_deterministic_fallback(matched_groups: list[str]) -> bool:
    return any(group.startswith("deterministic_") for group in matched_groups)


def _response_payload(response: AskResponse) -> dict:
    enriched = _enrich_response_with_context(response)
    return enriched.model_dump(mode="json")


@app.post("/ask/stream", tags=["generation"])
async def ask_stream_endpoint(
    http_request: Request,
    request: AskRequest,
) -> StreamingResponse:
    pool = await _require_pool(http_request)
    top_k = request.top_k if request.top_k else settings.top_k
    request_id = _resolve_request_id(request.request_id)
    _bind_request_context(request_id=request_id, endpoint="/ask/stream")
    started = time.monotonic()
    trace_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    trace_recorder = TraceRecorder(
        pool=pool,
        request_id=request_id,
        stream_queue=trace_queue,
        pipeline=getattr(http_request.app.state, "observability_pipeline", None),
    )
    set_current_trace_recorder(trace_recorder)

    async def event_stream() -> AsyncIterator[str]:
        stage_latencies_ms: dict[str, int] = {}
        await trace_recorder.emit(
            stage="request_received",
            status="started",
            message="Received streaming ask request.",
            details={"endpoint": "/ask/stream", "top_k": top_k},
            input_summary={"query_preview": summarize_text(request.query), "top_k": top_k},
        )
        yield _json_event(
            "started",
            message="Received question.",
            query=request.query,
            top_k=top_k,
            request_id=request_id,
        )
        for event in await _drain_trace_queue(trace_queue):
            yield _json_event("trace", **event)

        yield _json_event(
            "sql_generation_started",
            message="Retrieving schema context and generating guarded SQL.",
        )
        sql_started = time.monotonic()
        sql_task = asyncio.create_task(
            generate_sql(
                request.query,
                pool,
                settings,
                top_k,
                trace_callback=trace_recorder.emit,
            )
        )
        last_running_notice = time.monotonic()
        while True:
            done, _ = await asyncio.wait(
                {sql_task},
                timeout=min(
                    settings.ask_timeout_clamp_seconds,
                    _remaining_timeout_seconds(started, settings.ask_timeout),
                ),
            )
            for event in await _drain_trace_queue(trace_queue):
                yield _json_event("trace", **event)
            if done:
                break
            if time.monotonic() - started >= settings.ask_timeout:
                sql_task.cancel()
                with suppress(asyncio.CancelledError):
                    await sql_task
                timeout_warning = _ask_stream_timeout_warning(
                    "SQL generation",
                    settings.ask_timeout,
                )
                response = AskRejected(
                    sql=None,
                    warnings=[timeout_warning],
                    attempt_count=0,
                    react_trace=None,
                    stage_latencies_ms=stage_latencies_ms,
                )
                elapsed = _elapsed_ms(started)
                asyncio.create_task(
                    _log_request_event(
                        pool,
                        request_id=request_id,
                        endpoint="/ask/stream",
                        query_text=request.query,
                        top_k=top_k,
                        status=response.status,
                        attempt_count=None,
                        latency_ms=elapsed,
                        stage_latencies_ms=stage_latencies_ms,
                        warning_codes=[timeout_warning.code.value],
                        error_source="service_timeout",
                        metadata={"failed_stage": "sql_generation"},
                    )
                )
                asyncio.create_task(
                    _log_failure_event(
                        pool,
                        request_id=request_id,
                        endpoint="/ask/stream",
                        query_text=request.query,
                        warning_codes=[timeout_warning.code.value],
                        error_source="service_timeout",
                        sql_preview=None,
                        tables_attempted=[],
                        latency_ms=elapsed,
                        trace_emit=trace_recorder.emit,
                    )
                )
                yield _json_event(
                    "timeout",
                    message=timeout_warning.message,
                    warnings=_warning_payload(response.warnings),
                )
                await trace_recorder.emit(
                    stage="complete",
                    status=response.status,
                    message=timeout_warning.message,
                    duration_ms=elapsed,
                    warning_codes=[timeout_warning.code.value],
                    error_source="service_timeout",
                    details={
                        "failed_stage": "sql_generation",
                        "stage_latencies_ms": stage_latencies_ms,
                    },
                )
                for event in await _drain_trace_queue(trace_queue):
                    yield _json_event("trace", **event)
                yield _json_event("final", response=_response_payload(response))
                return
            if time.monotonic() - last_running_notice >= 10:
                yield _json_event(
                    "sql_generation_running",
                    message="Still generating and validating SQL.",
                )
                last_running_notice = time.monotonic()
        sql_result = await sql_task
        for event in await _drain_trace_queue(trace_queue):
            yield _json_event("trace", **event)
        stage_latencies_ms["sql_generation"] = _elapsed_ms(sql_started)
        merged_sql_latencies = {
            **(getattr(sql_result, "stage_latencies_ms", None) or {}),
            **stage_latencies_ms,
        }
        if getattr(sql_result, "stage_latencies_ms", None) != merged_sql_latencies:
            sql_result = sql_result.model_copy(update={"stage_latencies_ms": merged_sql_latencies})
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
            await trace_recorder.emit(
                stage="complete",
                status=sql_result.status,
                message=sql_result.failure_reason,
                duration_ms=_elapsed_ms(started),
                error_source="clarification",
                details={
                    "suggestion_count": len(sql_result.suggestions),
                    "stage_latencies_ms": stage_latencies_ms,
                },
            )
            for event in await _drain_trace_queue(trace_queue):
                yield _json_event("trace", **event)
            yield _json_event("final", response=_response_payload(sql_result))
            return
        if sql_result.status == "rejected":
            response = AskRejected(
                sql=None,
                warnings=sql_result.warnings,
                attempt_count=sql_result.attempt_count,
                react_trace=sql_result.react_trace,
                stage_latencies_ms={
                    **(sql_result.stage_latencies_ms or {}),
                    **stage_latencies_ms,
                },
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
            await trace_recorder.emit(
                stage="complete",
                status=response.status,
                message="Streaming ask stopped during SQL generation.",
                duration_ms=_elapsed_ms(started),
                warning_codes=[warning.code.value for warning in response.warnings],
                error_source=_derive_error_source(response.warnings),
                details={"stage_latencies_ms": stage_latencies_ms},
            )
            for event in await _drain_trace_queue(trace_queue):
                yield _json_event("trace", **event)
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
        await trace_recorder.emit(
            stage="execution",
            status="started",
            message="Executing bounded SQL on MySQL.",
            details={
                "tables_used": sql_result.tables_used,
                "sql_preview": capped_sql[:500],
            },
        )
        for event in await _drain_trace_queue(trace_queue):
            yield _json_event("trace", **event)
        execution_started = time.monotonic()
        try:
            columns, rows, execution_warnings = await asyncio.wait_for(
                mysql_executor.execute_sql(
                    sql=capped_sql,
                    settings=settings,
                ),
                timeout=_remaining_timeout_seconds(started, settings.ask_timeout),
            )
        except asyncio.TimeoutError:
            stage_latencies_ms["execution"] = _elapsed_ms(execution_started)
            timeout_warning = _ask_stream_timeout_warning(
                "MySQL execution",
                settings.ask_timeout,
            )
            response = AskRejected(
                sql=capped_sql,
                warnings=[*sql_result.warnings, timeout_warning],
                attempt_count=sql_result.attempt_count,
                react_trace=sql_result.react_trace,
                stage_latencies_ms={
                    **(sql_result.stage_latencies_ms or {}),
                    **stage_latencies_ms,
                },
            )
            elapsed = _elapsed_ms(started)
            asyncio.create_task(
                _log_request_event(
                    pool,
                    request_id=request_id,
                    endpoint="/ask/stream",
                    query_text=request.query,
                    top_k=top_k,
                    status=response.status,
                    attempt_count=response.attempt_count,
                    latency_ms=elapsed,
                    stage_latencies_ms=stage_latencies_ms,
                    warning_codes=[warning.code.value for warning in response.warnings],
                    error_source="service_timeout",
                    metadata={
                        "failed_stage": "execution",
                        "sql_present": response.sql is not None,
                        "tables_used": sql_result.tables_used,
                        "matched_groups": sql_result.matched_groups,
                    },
                )
            )
            asyncio.create_task(
                _log_failure_event(
                    pool,
                    request_id=request_id,
                    endpoint="/ask/stream",
                    query_text=request.query,
                    warning_codes=[warning.code.value for warning in response.warnings],
                    error_source="service_timeout",
                    sql_preview=capped_sql,
                    tables_attempted=sql_result.tables_used,
                    latency_ms=elapsed,
                    trace_emit=trace_recorder.emit,
                )
            )
            yield _json_event(
                "timeout",
                message=timeout_warning.message,
                warnings=_warning_payload([timeout_warning]),
            )
            await trace_recorder.emit(
                stage="execution",
                status="failed",
                message=timeout_warning.message,
                duration_ms=stage_latencies_ms["execution"],
                warning_codes=[timeout_warning.code.value],
                error_source="service_timeout",
                details={"sql_preview": capped_sql[:500]},
            )
            await trace_recorder.emit(
                stage="complete",
                status=response.status,
                message=timeout_warning.message,
                duration_ms=elapsed,
                warning_codes=[warning.code.value for warning in response.warnings],
                error_source="service_timeout",
                details={
                    "failed_stage": "execution",
                    "stage_latencies_ms": response.stage_latencies_ms or {},
                },
            )
            for event in await _drain_trace_queue(trace_queue):
                yield _json_event("trace", **event)
            yield _json_event("final", response=_response_payload(response))
            return
        stage_latencies_ms["execution"] = _elapsed_ms(execution_started)
        await trace_recorder.emit(
            stage="execution",
            status="completed" if not execution_warnings else "failed",
            message=(
                "SQL execution completed."
                if not execution_warnings
                else "SQL execution failed."
            ),
            duration_ms=stage_latencies_ms["execution"],
            warning_codes=[warning.code.value for warning in execution_warnings],
            error_source="execution" if execution_warnings else None,
            details={
                "row_count": len(rows),
                "columns": columns,
                "sql_preview": capped_sql[:500],
            },
        )
        for event in await _drain_trace_queue(trace_queue):
            yield _json_event("trace", **event)
        if execution_warnings:
            response = AskRejected(
                sql=capped_sql,
                warnings=[*sql_result.warnings, *execution_warnings],
                attempt_count=sql_result.attempt_count,
                react_trace=sql_result.react_trace,
                stage_latencies_ms={
                    **(sql_result.stage_latencies_ms or {}),
                    **stage_latencies_ms,
                },
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
            await trace_recorder.emit(
                stage="complete",
                status=response.status,
                message="Streaming ask stopped during SQL execution.",
                duration_ms=_elapsed_ms(started),
                warning_codes=[warning.code.value for warning in response.warnings],
                error_source=_derive_error_source(response.warnings),
                details={"stage_latencies_ms": response.stage_latencies_ms or {}},
            )
            for event in await _drain_trace_queue(trace_queue):
                yield _json_event("trace", **event)
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
        await trace_recorder.emit(
            stage="answer_generation",
            status="started",
            message="Generating final answer from result rows.",
            details={"row_count": len(rows), "columns": columns},
        )
        for event in await _drain_trace_queue(trace_queue):
            yield _json_event("trace", **event)
        answer_started = time.monotonic()
        if _should_use_deterministic_fallback(sql_result.matched_groups):
            answer_text = answer_generator.build_fallback_answer(
                query=request.query,
                columns=columns,
                rows=rows,
                row_count=len(rows),
            )
            answer_warnings = []
        else:
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
            last_answer_notice = time.monotonic()
            while True:
                done, _ = await asyncio.wait(
                    {answer_task},
                    timeout=min(
                        settings.ask_timeout_clamp_seconds,
                        _remaining_timeout_seconds(started, settings.ask_timeout),
                    ),
                )
                for event in await _drain_trace_queue(trace_queue):
                    yield _json_event("trace", **event)
                if done:
                    break
                if time.monotonic() - started >= settings.ask_timeout:
                    answer_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await answer_task
                    stage_latencies_ms["answer_generation"] = _elapsed_ms(answer_started)
                    timeout_warning = _ask_stream_timeout_warning(
                        "answer generation",
                        settings.ask_timeout,
                    )
                    response = AskRejected(
                        sql=capped_sql,
                        warnings=[*sql_result.warnings, timeout_warning],
                        attempt_count=sql_result.attempt_count,
                        react_trace=sql_result.react_trace,
                        stage_latencies_ms={
                            **(sql_result.stage_latencies_ms or {}),
                            **stage_latencies_ms,
                        },
                    )
                    elapsed = _elapsed_ms(started)
                    asyncio.create_task(
                        _log_request_event(
                            pool,
                            request_id=request_id,
                            endpoint="/ask/stream",
                            query_text=request.query,
                            top_k=top_k,
                            status=response.status,
                            attempt_count=response.attempt_count,
                            latency_ms=elapsed,
                            stage_latencies_ms=stage_latencies_ms,
                            warning_codes=[warning.code.value for warning in response.warnings],
                            error_source="service_timeout",
                            metadata={
                                "failed_stage": "answer_generation",
                                "sql_present": response.sql is not None,
                                "row_count": len(rows),
                                "tables_used": sql_result.tables_used,
                                "matched_groups": sql_result.matched_groups,
                            },
                        )
                    )
                    asyncio.create_task(
                        _log_failure_event(
                            pool,
                            request_id=request_id,
                            endpoint="/ask/stream",
                            query_text=request.query,
                            warning_codes=[warning.code.value for warning in response.warnings],
                            error_source="service_timeout",
                            sql_preview=capped_sql,
                            tables_attempted=sql_result.tables_used,
                            latency_ms=elapsed,
                            trace_emit=trace_recorder.emit,
                        )
                    )
                    yield _json_event(
                        "timeout",
                        message=timeout_warning.message,
                        warnings=_warning_payload([timeout_warning]),
                    )
                    await trace_recorder.emit(
                        stage="answer_generation",
                        status="failed",
                        message=timeout_warning.message,
                        duration_ms=stage_latencies_ms["answer_generation"],
                        warning_codes=[timeout_warning.code.value],
                        error_source="service_timeout",
                    )
                    await trace_recorder.emit(
                        stage="complete",
                        status=response.status,
                        message=timeout_warning.message,
                        duration_ms=elapsed,
                        warning_codes=[warning.code.value for warning in response.warnings],
                        error_source="service_timeout",
                        details={
                            "failed_stage": "answer_generation",
                            "stage_latencies_ms": response.stage_latencies_ms or {},
                        },
                    )
                    for event in await _drain_trace_queue(trace_queue):
                        yield _json_event("trace", **event)
                    yield _json_event("final", response=_response_payload(response))
                    return
                if time.monotonic() - last_answer_notice >= 10:
                    yield _json_event(
                        "answer_generation_running",
                        message="Still generating final answer.",
                    )
                    last_answer_notice = time.monotonic()
            answer_text, answer_warnings = await answer_task
        stage_latencies_ms["answer_generation"] = _elapsed_ms(answer_started)
        await trace_recorder.emit(
            stage="answer_generation",
            status="completed" if answer_text is not None else "failed",
            message=(
                "Answer generation completed."
                if answer_text is not None
                else "Answer generation failed."
            ),
            duration_ms=stage_latencies_ms["answer_generation"],
            warning_codes=[warning.code.value for warning in answer_warnings],
            error_source="answer_generation" if answer_text is None else None,
        )
        for event in await _drain_trace_queue(trace_queue):
            yield _json_event("trace", **event)
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
                stage_latencies_ms={
                    **(sql_result.stage_latencies_ms or {}),
                    **stage_latencies_ms,
                },
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
            await trace_recorder.emit(
                stage="complete",
                status=response.status,
                message="Streaming ask stopped during answer generation.",
                duration_ms=_elapsed_ms(started),
                warning_codes=[warning.code.value for warning in response.warnings],
                error_source=_derive_error_source(response.warnings),
                details={"stage_latencies_ms": response.stage_latencies_ms or {}},
            )
            for event in await _drain_trace_queue(trace_queue):
                yield _json_event("trace", **event)
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
            stage_latencies_ms={
                **(sql_result.stage_latencies_ms or {}),
                **stage_latencies_ms,
            },
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
        await trace_recorder.emit(
            stage="complete",
            status=response.status,
            message="Streaming ask request completed.",
            duration_ms=_elapsed_ms(started),
            warning_codes=[warning.code.value for warning in response.warnings],
            error_source=_derive_error_source(response.warnings),
            details={
                "stage_latencies_ms": response.stage_latencies_ms or {},
                "row_count": response.row_count,
                "tables_used": response.tables_used,
                "matched_groups": response.matched_groups,
            },
        )
        for event in await _drain_trace_queue(trace_queue):
            yield _json_event("trace", **event)
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
