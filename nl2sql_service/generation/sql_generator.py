from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Protocol

import asyncpg
import sqlparse
from sqlparse import tokens as T
from sqlparse.sql import Comment, Identifier, IdentifierList, Parenthesis, Statement, Token, TokenList

from nl2sql_service.generation import query_rewriter
from nl2sql_service.rag import retrieve
from nl2sql_service.db import db
from nl2sql_service.core.cache import sql_cache
from nl2sql_service.core.cache import semantic_sql_cache
from nl2sql_service.db.column_loader import load_columns_for_tables
from nl2sql_service.core.config import Settings, settings as default_settings
from nl2sql_service.llm import get_model_client
from nl2sql_service.models import (
    CacheSource,
    ReActAction,
    ReactTrace,
    GenerateSqlClarification,
    GenerateSqlRejected,
    GenerateSqlResponse,
    GenerateSqlSuccess,
    SqlWarning,
    WarningCode,
)
from nl2sql_service.observability.context import emit_current_trace_event
from nl2sql_service.observability.sanitization import sanitize_sql, stable_hash, summarize_text
from nl2sql_service.core.roles import LLMRole
from nl2sql_service.core.rulebook import build_governance_block, get_config

logger = logging.getLogger(__name__)

TraceCallback = Callable[..., Awaitable[None]]
_EMAIL_LITERAL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_INTEGER_LITERAL_RE = re.compile(r"\b\d+\b")

@dataclass(frozen=True)
class ColumnSearchHit:
    table_name: str
    column_name: str
    similarity: float = 0.0

class VectorStore(Protocol):
    async def search_columns(
        self,
        *,
        embedding: list[float],
        top_k: int,
    ) -> list[ColumnSearchHit]:
        ...

# TODO: Move concrete vector-store calls up to the route/service layer and pass
# list[ColumnSearchHit] into this module so SQL generation depends only on data,
# not the pgvector adapter.
_COLUMN_SIMILARITY_SQL = """
SELECT
    1 - (embedding <=> $1) AS similarity,
    metadata
FROM nl2sql_embeddings
WHERE metadata->>'type' = 'column_catalog'
ORDER BY embedding <=> $1
LIMIT $2
"""

class PgVectorStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def search_columns(
        self,
        *,
        embedding: list[float],
        top_k: int,
    ) -> list[ColumnSearchHit]:
        async with self._pool.acquire() as conn:
            rows = await retrieve._fetch_vector_rows(
                conn,
                _COLUMN_SIMILARITY_SQL,
                embedding,
                top_k,
            )

        hits: list[ColumnSearchHit] = []
        for row in rows:
            raw = row["metadata"]
            if not raw:
                metadata: dict[str, Any] = {}
            elif isinstance(raw, str):
                metadata = json.loads(raw)
            else:
                metadata = dict(raw)
            table_name = _normalize_table_name(
                str(metadata.get("table_name") or metadata.get("object_name") or "")
            ).lower()
            column_name = _normalize_table_name(str(metadata.get("column_name") or ""))
            if not table_name or not column_name:
                continue
            hits.append(
                ColumnSearchHit(
                    table_name=table_name,
                    column_name=column_name,
                    similarity=float(row["similarity"]),
                )
            )
        return hits

class _StaticColumnSearchVectorStore:
    def __init__(self, hits: list[ColumnSearchHit]):
        self._hits = hits

    async def search_columns(
        self,
        *,
        embedding: list[float],
        top_k: int,
    ) -> list[ColumnSearchHit]:
        del embedding
        return self._hits[:top_k]

async def _emit_trace(
    trace_callback: TraceCallback | None,
    *,
    stage: str,
    status: str,
    message: str,
    duration_ms: int | None = None,
    warning_codes: list[str] | None = None,
    error_source: str | None = None,
    details: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    if trace_callback is None:
        return
    await trace_callback(
        stage=stage,
        status=status,
        message=message,
        duration_ms=duration_ms,
        warning_codes=warning_codes,
        error_source=error_source,
        details=details or {},
        **extra,
    )

_FENCED_SQL_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_RAW_SQL_START_RE = re.compile(
    r"^\s*(SELECT|WITH|DROP|DELETE|TRUNCATE|INSERT|UPDATE|ALTER|CREATE|"
    r"REPLACE|GRANT|REVOKE|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)
_SIMPLE_SELECT_STAR_RE = re.compile(
    r"^(?P<prefix>\s*SELECT\s+)\*\s+(?P<from>FROM\s+)(?P<table>`?[A-Za-z_][A-Za-z0-9_$]*`?)(?P<rest>\s+.*|\s*)$",
    re.IGNORECASE | re.DOTALL,
)
_SQL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_DESTRUCTIVE_KEYWORDS = {
    "DROP",
    "DELETE",
    "TRUNCATE",
    "INSERT",
    "UPDATE",
    "ALTER",
    "CREATE",
    "REPLACE",
    "GRANT",
    "REVOKE",
    "EXEC",
    "EXECUTE",
}
_CLAUSE_END_KEYWORDS = {
    "WHERE",
    "ON",
    "GROUP",
    "GROUP BY",
    "ORDER",
    "ORDER BY",
    "HAVING",
    "LIMIT",
    "OFFSET",
    "UNION",
    "EXCEPT",
    "INTERSECT",
    "QUALIFY",
    "WINDOW",
}
_COLUMN_NAME_SKIP_KEYWORDS = {
    "ALL",
    "AND",
    "AS",
    "ASC",
    "BETWEEN",
    "BY",
    "CASE",
    "CAST",
    "COUNT",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "DATE",
    "DAY",
    "DESC",
    "DISTINCT",
    "ELSE",
    "END",
    "EXISTS",
    "FALSE",
    "FROM",
    "GROUP",
    "HAVING",
    "IN",
    "INNER",
    "INTERVAL",
    "IS",
    "JOIN",
    "LEFT",
    "LIKE",
    "LIMIT",
    "MONTH",
    "NOT",
    "NULL",
    "ON",
    "OR",
    "ORDER",
    "OUTER",
    "RIGHT",
    "SELECT",
    "SUM",
    "THEN",
    "TRUE",
    "WHEN",
    "WHERE",
    "WITH",
    "YEAR",
}
_RECENT_QUERY_TERMS = ("recent", "latest", "newest", "last")
_COUNT_RE = re.compile(r"\b(\d{1,3})\b")
COLUMN_SELECTION_RULE = """COLUMN SELECTION RULE:
 For listing queries (show, list, find, get, fetch):
   SELECT: id, name/title/subject columns, status,
           and the most relevant date column only.
   Do NOT select: financial columns (amount, balance,
   fee, cost), audit columns (created_by, updated_by,
   allocation_date, last_updated), or internal columns
   unless the user specifically asks for them.
For aggregation queries (total, count, sum, average):
  SELECT only the columns needed for the calculation.
 For detail queries (details, full, all columns, *):
   SELECT * is acceptable."""

def _parse_csv_terms(value: str) -> set[str]:
    return {
        item.strip().lower()
        for item in value.split(",")
        if item.strip()
    }

def _load_deterministic_filter_rules(settings: Settings) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(settings.deterministic_filter_rules_json)
    except json.JSONDecodeError:
        logger.warning("Invalid DETERMINISTIC_FILTER_RULES_JSON; deterministic filters disabled.")
        return []
    if not isinstance(parsed, list):
        return []
    return [rule for rule in parsed if isinstance(rule, dict)]

def _table_label(table: str) -> str:
    return _normalize_table_name(table).replace("_", " ").strip()

def _singularize_label(label: str) -> str:
    if label.endswith("ies"):
        return f"{label[:-3]}y"
    if label.endswith("s") and not label.endswith("ss"):
        return label[:-1]
    return label

def _pluralize_label(label: str) -> str:
    if label.endswith("y") and not label.endswith(("ay", "ey", "iy", "oy", "uy")):
        return f"{label[:-1]}ies"
    if label.endswith("s"):
        return label
    return f"{label}s"

def _build_schema_derived_suggestions(
    tables_in_scope: list[str],
    *,
    limit: int = 3,
) -> list[str]:
    suggestions: list[str] = []
    normalized_tables: list[str] = []
    for table in tables_in_scope:
        label = _table_label(table)
        if label and label not in normalized_tables:
            normalized_tables.append(label)

    for label in normalized_tables:
        plural = _pluralize_label(label)
        singular = _singularize_label(label)
        for suggestion in (
            f"Show recent {plural}",
            f"List {plural}",
            f"Find {singular} by a specific column value",
        ):
            if suggestion not in suggestions:
                suggestions.append(suggestion)
            if len(suggestions) >= limit:
                return suggestions

    fallback_suggestions = [
        "List records from a specific table",
        "Show recent rows from one table",
        "Find a row by a specific column value",
    ]
    for suggestion in fallback_suggestions:
        if suggestion not in suggestions:
            suggestions.append(suggestion)
        if len(suggestions) >= limit:
            break
    return suggestions

def _quote_identifier(identifier: str) -> str:
    if _SQL_NAME_RE.match(identifier):
        return identifier
    return f"`{identifier.replace('`', '``')}`"

def _query_looks_multi_table(query: str) -> bool:
    normalized = " ".join(query.lower().split())
    if re.search(r"\b(join|compare|between|alongside|versus|vs)\b", normalized):
        return True
    return bool(re.search(r",|\band\b|\bwith\b|\bby\b", normalized))

async def _schema_is_self_contained(
    query: str,
    query_embedding: list[float],
    candidate_table: str,
    retrieved_columns: list[str],
    known_schema_tables: list[str],
    matched_groups: list[str],
    top_k: int,
    vector_store: VectorStore,
) -> bool:
    del retrieved_columns
    top_columns = await vector_store.search_columns(
        embedding=query_embedding,
        top_k=top_k,
    )
    candidate = _normalize_table_name(candidate_table).lower()
    known_tables = {
        _normalize_table_name(table).lower()
        for table in known_schema_tables
        if _normalize_table_name(table)
    }
    known_groups = {
        _normalize_table_name(group).lower()
        for group in matched_groups
        if _normalize_table_name(group)
    }
    if candidate not in known_tables and candidate not in known_groups:
        await emit_current_trace_event(
            stage="deterministic_candidate_check",
            status="warning",
            message="Candidate table was not confirmed by retrieved schema context",
            details={
                "candidate_table": candidate,
                "known_schema_tables": sorted(known_tables),
                "matched_groups": sorted(known_groups),
            },
        )
        return False

    candidate_hits = [
        column
        for column in top_columns
        if _normalize_table_name(column.table_name).lower() == candidate
    ]
    foreign_table_hits = [
        column
        for column in top_columns
        if _normalize_table_name(column.table_name).lower() != candidate
    ]
    if not foreign_table_hits:
        return True

    dominant_ratio = len(candidate_hits) / max(1, len(top_columns))
    foreign_tables = {
        _normalize_table_name(column.table_name).lower()
        for column in foreign_table_hits
    }
    return (
        len(candidate_hits) >= 2
        and dominant_ratio >= 0.6
        and len(foreign_tables) <= 1
        and not _query_looks_multi_table(query)
    )

async def is_deterministic_generation_candidate(
    query: str,
    query_embedding: list[float],
    settings: Settings,
    vector_store: VectorStore,
    known_schema_tables: list[str],
    matched_groups: list[str] | None = None,
) -> bool:
    top_k = max(1, settings.top_k)
    top_columns = await vector_store.search_columns(
        embedding=query_embedding,
        top_k=top_k,
    )
    if not top_columns:
        normalized_known_tables = [
            _normalize_table_name(table).lower()
            for table in known_schema_tables
            if _normalize_table_name(table)
        ]
        if (
            len(normalized_known_tables) == 1
            and not _query_looks_multi_table(query)
            and _query_mentions_table_form(query, normalized_known_tables[0], plural=True)
        ):
            await emit_current_trace_event(
                stage="deterministic_candidate_check",
                status="passed",
                message="Single-table schema confirmation allowed the deterministic fast path without column hits",
                details={"candidate_table": normalized_known_tables[0]},
            )
            return True
        await emit_current_trace_event(
            stage="deterministic_candidate_check",
            status="skipped",
            message="No column similarity signals available",
            details={"foreign_table_hits": []},
        )
        return False

    candidate_table = _normalize_table_name(top_columns[0].table_name).lower()
    retrieved_columns = [
        _normalize_table_name(column.column_name)
        for column in top_columns
        if _normalize_table_name(column.table_name).lower() == candidate_table
    ]
    foreign_table_hits = sorted(
        {
            _normalize_table_name(column.table_name).lower()
            for column in top_columns
            if _normalize_table_name(column.table_name).lower() != candidate_table
        }
    )
    self_contained = await _schema_is_self_contained(
        query=query,
        query_embedding=query_embedding,
        candidate_table=candidate_table,
        retrieved_columns=retrieved_columns,
        known_schema_tables=known_schema_tables,
        matched_groups=matched_groups or [],
        top_k=top_k,
        vector_store=_StaticColumnSearchVectorStore(top_columns),
    )
    if not self_contained:
        await emit_current_trace_event(
            stage="deterministic_candidate_check",
            status="skipped",
            message="Column similarity signals were not dominant enough for a fast-path candidate",
            details={
                "candidate_table": candidate_table,
                "retrieved_columns": retrieved_columns,
                "foreign_table_hits": foreign_table_hits,
            },
        )
        return False

    await emit_current_trace_event(
        stage="deterministic_candidate_check",
        status="passed",
        message="Column similarity signals are self-contained",
        details={
            "candidate_table": candidate_table,
            "retrieved_columns": retrieved_columns,
            "known_schema_tables": [
                _normalize_table_name(table).lower()
                for table in known_schema_tables
                if _normalize_table_name(table)
            ],
            "foreign_table_hits": [],
        },
    )
    return True

def _deterministic_limit(query: str, top_k: int, target_table: str | None = None) -> int:
    match = _COUNT_RE.search(query)
    if match:
        return max(1, min(int(match.group(1)), 50))

    if target_table and _query_mentions_table_form(query, target_table, plural=False):
        return 1
    return max(1, min(top_k, 50))

def _best_recent_order_columns(columns: list[str]) -> list[str]:
    lookup = {column.lower(): column for column in columns}
    ordered: list[str] = []
    for preferred in (
        "created_at",
        "updated_at",
        "date",
        "modified_at",
        "last_updated",
        "id",
    ):
        column = lookup.get(preferred)
        if column:
            ordered.append(column)

    for column in columns:
        column_lower = column.lower()
        if column in ordered:
            continue
        if any(fragment in column_lower for fragment in ("date", "time", "_at")):
            ordered.append(column)
        if len(ordered) >= 4:
            break
    return ordered

def _select_listing_columns(
    columns: list[str],
    order_columns: list[str],
    max_columns: int = 8,
) -> list[str]:
    selected: list[str] = []

    def add(column: str) -> None:
        if column not in selected and len(selected) < max_columns:
            selected.append(column)

    for column in columns:
        column_lower = column.lower()
        if column_lower == "id" or column_lower.endswith("_id"):
            add(column)

    for column in columns:
        column_lower = column.lower()
        if any(
            fragment in column_lower
            for fragment in (
                "name",
                "title",
                "subject",
                "status",
                "type",
                "number",
                "no",
                "code",
                "amount",
                "total",
            )
        ):
            add(column)

    for column in order_columns[:1]:
        add(column)

    for column in columns:
        if len(selected) >= max_columns:
            break
        column_lower = column.lower()
        if _is_audit_column(column_lower):
            continue
        add(column)

    return (selected or columns[:max_columns])[:max_columns]

def _is_audit_column(column_lower: str) -> bool:
    return column_lower in {
        "created_by",
        "created_at",
        "creator",
        "creator_id",
        "last_updated",
        "modified_at",
        "modified_by",
        "updated_at",
        "updated_by",
    }

def _query_mentions_column(query_lower: str, column_lower: str) -> bool:
    return column_lower in query_lower or column_lower.replace("_", " ") in query_lower

def _normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

def _singularize(term: str) -> str:
    if len(term) > 3 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 3 and term.endswith("ses"):
        return term[:-2]
    if len(term) > 2 and term.endswith("s"):
        return term[:-1]
    return term

def _pluralize(term: str) -> str:
    if term.endswith("y") and len(term) > 1 and term[-2] not in "aeiou":
        return term[:-1] + "ies"
    if term.endswith("s"):
        return term + "es"
    return term + "s"

def _table_aliases(table: str) -> set[str]:
    normalized = _normalize_match_text(table)
    tokens = [token for token in normalized.split() if token]
    aliases = {normalized}
    aliases.update(tokens)
    aliases.update(_singularize(token) for token in tokens)
    aliases.update(_pluralize(_singularize(token)) for token in tokens)
    if len(tokens) > 1:
        singular_last = _singularize(tokens[-1])
        aliases.add(" ".join([*tokens[:-1], singular_last]))
        aliases.add(" ".join([*tokens[:-1], _pluralize(singular_last)]))
    return {alias for alias in aliases if alias}

def _query_mentions_table_form(
    query: str,
    table: str,
    *,
    plural: bool | None = None,
) -> bool:
    query_text = f" {_normalize_match_text(query)} "
    for alias in _table_aliases(table):
        if plural is True and alias != _pluralize(_singularize(alias)):
            continue
        if plural is False and alias != _singularize(alias):
            continue
        if f" {alias} " in query_text:
            return True
    return False

def _choose_explicit_table(query: str, tables: list[str]) -> str | None:
    query_text = f" {_normalize_match_text(query)} "
    scored: list[tuple[int, int, str]] = []
    for index, table in enumerate(tables):
        table_normalized = _normalize_match_text(table)
        score = 0
        if table_normalized and f" {table_normalized} " in query_text:
            score += 500 + len(table_normalized)
        for alias in _table_aliases(table):
            if f" {alias} " in query_text:
                score += (700 if " " in alias else 100) + len(alias)
        if score > 0:
            scored.append((-score, index, table))

    if not scored:
        return None
    return sorted(scored)[0][2]

def _find_column_by_fragments(columns: list[str], *fragments: str) -> str | None:
    lowered = {column.lower(): column for column in columns}
    for fragment in fragments:
        if fragment in lowered:
            return lowered[fragment]
    for column in columns:
        column_lower = column.lower()
        if any(fragment in column_lower for fragment in fragments):
            return column
    return None

def _build_deterministic_filter_clause(
    query: str,
    columns: list[str],
    settings: Settings,
) -> str | None:
    query_lower = query.lower()
    for rule in _load_deterministic_filter_rules(settings):
        query_terms = [str(term).strip().lower() for term in rule.get("query_terms", []) if str(term).strip()]
        column_terms = [str(term).strip().lower() for term in rule.get("column_terms", []) if str(term).strip()]
        if query_terms and not any(re.search(rf"\b{re.escape(term)}\b", query_lower) for term in query_terms):
            continue
        target_column = _find_column_by_fragments(columns, *column_terms)
        if not target_column:
            continue
        literal_type = str(rule.get("literal_type") or "").strip().lower()
        fallback = str(rule.get("fallback") or "").strip().lower()
        if literal_type == "email" and not _query_mentions_column(query_lower, target_column.lower()):
            continue
        if literal_type == "integer" and not _query_mentions_column(query_lower, target_column.lower()):
            continue

        if literal_type == "email":
            email_match = _EMAIL_LITERAL_RE.search(query)
            if email_match:
                return f"{_quote_identifier(target_column)} = '{email_match.group(0)}'"
            if fallback == "not_null":
                return f"{_quote_identifier(target_column)} IS NOT NULL"
            continue

        if literal_type == "integer":
            number_match = _INTEGER_LITERAL_RE.search(query)
            if number_match:
                return f"{_quote_identifier(target_column)} = {number_match.group(0)}"
            if fallback == "not_null":
                return f"{_quote_identifier(target_column)} IS NOT NULL"
            continue

        operator = str(rule.get("operator") or "=").strip() or "="
        value = rule.get("value")
        if value is None:
            continue
        escaped_value = str(value).replace("'", "''")
        return f"{_quote_identifier(target_column)} {operator} '{escaped_value}'"

    return None

def _has_blocking_warnings(warnings: list[SqlWarning]) -> bool:
    return any(warning.code != WarningCode.MYSQL_EXPLAIN_UNAVAILABLE for warning in warnings)

def _with_cache_metadata(payload: dict[str, Any], source: CacheSource) -> dict[str, Any]:
    updated = dict(payload)
    updated["cache_hit"] = source != CacheSource.NONE
    updated["cache_source"] = source.value
    return updated

async def _load_query_embedding(
    query: str,
) -> list[float] | None:
    from nl2sql_service.core.cache import embed_cache
    from nl2sql_service import embed as embed_module

    q_vec = embed_cache.get(query)
    if q_vec is not None:
        return q_vec

    vecs = await embed_module.embed_texts([query])
    if not vecs:
        return None
    q_vec = vecs[0]
    embed_cache.set(query, q_vec)
    return q_vec

def build_deterministic_sql(
    query: str,
    allowed_columns: dict[str, list[str]],
    top_k: int,
    settings: Settings | None = None,
) -> tuple[str, list[str]] | None:
    """Return validated-template SQL for high-confidence simple intents."""
    active_settings = settings or default_settings
    query_lower = query.lower()
    tables = list(allowed_columns)
    target_table = _choose_explicit_table(query, tables)
    if target_table is None:
        return None

    columns = allowed_columns.get(target_table) or allowed_columns.get(target_table.lower())
    if not columns:
        return None

    is_recent_style = (
        _COUNT_RE.search(query) is not None
        or any(term in query_lower for term in _RECENT_QUERY_TERMS)
    )
    order_columns = _best_recent_order_columns(columns)
    selected_columns = _select_listing_columns(
        columns=columns,
        order_columns=order_columns if is_recent_style else [],
        max_columns=8,
    )
    if not selected_columns:
        return None

    limit = _deterministic_limit(query, top_k, target_table=target_table)
    select_list = ", ".join(_quote_identifier(column) for column in selected_columns)
    sql = f"SELECT {select_list} FROM {_quote_identifier(target_table)}"
    if is_recent_style:
        if not order_columns:
            return None
        order_by = ", ".join(f"{_quote_identifier(column)} DESC" for column in order_columns)
        sql = f"{sql} ORDER BY {order_by} LIMIT {limit}"
    else:
        where_clause = _build_deterministic_filter_clause(query, columns, active_settings)
        if not where_clause:
            return None
        sql = f"{sql} WHERE {where_clause} LIMIT {limit}"
    return sql, [target_table]

def build_sql_prompt(
    query: str,
    context: str,
    tables_in_scope: list[str],
    dialect: str,
    allowed_columns: dict[str, list[str]] | None = None,
    planner_instruction: str = "",
    settings: Settings | None = None,
) -> str:
    active_settings = settings or default_settings
    tables = ", ".join(tables_in_scope) if tables_in_scope else "(none)"
    lines = [
        f"Generate ONE {dialect} SELECT statement only.",
        "Use read-only SQL. Do not execute the SQL.",
        f"Only use these tables: {tables}",
        (
            "For show/list queries, choose concise, semantically relevant columns. "
            "Use SELECT * only when the user explicitly asks for full details, "
            "all columns, or raw rows."
        ),
        COLUMN_SELECTION_RULE,
        "Honor explicit row counts with LIMIT.",
        "For latest/recent requests, order by the best available date or timestamp column.",
    ]
    if planner_instruction:
        lines.append(f"Planner instruction: {planner_instruction}")
    if allowed_columns:
        lines.extend(
            [
                "Only use these known columns:",
                *[
                    f"- {table}: {', '.join(columns)}"
                    for table, columns in allowed_columns.items()
                ],
            ]
        )
    if active_settings.governance_enabled and active_settings.governance_inject_sql:
        governance = build_governance_block(
            get_config(active_settings),
            context="sql_gen",
        )
        if governance:
            lines.extend(["", governance])
    lines.extend(
        [
            "",
            "User question:",
            query,
            "",
            "Schema context:",
            "```text",
            context,
            "```",
            "",
            "Return only the SQL. No explanation.",
        ]
    )
    return "\n".join(lines)

def build_refinement_prompt(
    query: str,
    context: str,
    tables_in_scope: list[str],
    dialect: str,
    previous_sql: str,
    validation_errors: list[SqlWarning],
    attempt: int,
    allowed_columns: dict[str, list[str]] | None = None,
    planner_instruction: str = "",
    settings: Settings | None = None,
) -> str:
    active_settings = settings or default_settings
    tables = ", ".join(tables_in_scope) if tables_in_scope else "(none)"
    errors = "\n".join(
        f"- {warning.code.value}: {warning.message}" for warning in validation_errors
    )
    if not errors:
        errors = "- UNKNOWN: Previous SQL did not pass validation."

    lines = [
        f"Your previous SQL attempt {attempt} failed.",
        "Validation errors:",
        errors,
        *(
            ["", f"Planner instruction: {planner_instruction}"]
            if planner_instruction
            else []
        ),
        "",
        "Previous SQL:",
        "```sql",
        previous_sql,
        "```",
        "",
        "Constraints:",
        f"- Generate ONE {dialect} SELECT statement only.",
        "- Use read-only SQL. Do not execute the SQL.",
        f"- Only use these tables: {tables}",
        (
            "- For show/list queries, choose concise, semantically relevant columns. "
            "Use SELECT * only when the user explicitly asks for full details, "
            "all columns, or raw rows."
        ),
        COLUMN_SELECTION_RULE,
        "- Honor explicit row counts with LIMIT.",
        "- For latest/recent requests, order by the best available date or timestamp column.",
        "- Correct every validation error listed above.",
        "- Do not reuse disallowed tables or columns from previous SQL.",
        "- If planner instruction conflicts with constraints, follow constraints.",
    ]
    if active_settings.governance_enabled and active_settings.governance_inject_sql:
        governance = build_governance_block(
            get_config(active_settings),
            context="sql_gen",
        )
        if governance:
            lines.extend(["", governance])
    lines.extend(
        [
            "",
            "User question:",
            query,
            "",
            "Schema context:",
            "```text",
            context,
            "```",
            "",
            "Return only the corrected SQL.",
        ]
    )
    return "\n".join(lines)

async def call_ollama(
    prompt: str,
    settings: Settings,
    timeout: float | None = None,
) -> tuple[str | None, list[SqlWarning]]:
    client = get_model_client(
        settings=settings,
        model=settings.sql_model or settings.llm_model,
        default_timeout=settings.llm_timeout,
        role=LLMRole.SQL.value,
    )
    await emit_current_trace_event(
        event="prompt_construction",
        stage="prompt_construction",
        status="completed",
        message="SQL prompt constructed.",
        provider=client.provider_name,
        model=client.model_name,
        input_summary={
            "prompt_hash": stable_hash(prompt),
            "prompt_chars": len(prompt),
            "prompt_preview": summarize_text(prompt, limit=min(settings.observability_prompt_char_limit, 500)),
        },
        metadata={"prompt_version": "sql_generator.v1"},
    )
    response = await client.generate(
        prompt=prompt,
        temperature=0.0,
        timeout=max(0.001, min(float(timeout or settings.llm_timeout), float(settings.llm_timeout))),
    )
    if not response.text:
        code = (
            WarningCode.OLLAMA_TIMEOUT
            if response.error_type == "timeout"
            else WarningCode.OLLAMA_MALFORMED
            if response.error_type in {"malformed", "empty"}
            else WarningCode.OLLAMA_UPSTREAM
        )
        detail = response.error_message or f"{client.provider_name} model returned no text"
        return None, [
            SqlWarning(
                code=code,
                message=detail,
            )
        ]

    await emit_current_trace_event(
        event="sql_generated",
        stage="sql_generation",
        status="completed",
        message="SQL text returned by model.",
        provider=response.provider or client.provider_name,
        model=response.model_name or client.model_name,
        duration_ms=response.latency_ms,
        token_usage={
            "total_tokens": response.tokens_used,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
        },
        output_summary={"raw_sql_preview": sanitize_sql(response.text, limit=settings.observability_sql_char_limit)},
    )
    return response.text, []

def extract_sql(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        return ""

    fenced_match = _FENCED_SQL_RE.search(stripped)
    if fenced_match:
        return fenced_match.group(1).strip()

    lines = stripped.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^\s*(SELECT|WITH)\b", line, re.IGNORECASE) or re.match(
            r"^\s*--",
            line,
        ):
            return "\n".join(lines[index:]).strip()

    if _RAW_SQL_START_RE.match(stripped):
        return stripped

    return ""

def narrow_select_star(
    sql: str,
    allowed_columns: dict[str, list[str]],
    query: str,
    max_columns: int = 8,
) -> str:
    """
    Replace simple single-table SELECT * with a focused column list.
    
    We now rely on exact column generation from RAG. The available columns
    in `allowed_columns` have already been pre-filtered by relevance. We
    simply apply the top `max_columns` to avoid huge result sets.
    """
    if _query_requests_all_columns(query):
        return sql

    match = _SIMPLE_SELECT_STAR_RE.match(sql.strip())
    if not match:
        return sql

    table_name = _normalize_table_name(match.group("table"))
    columns = allowed_columns.get(table_name.lower())
    if not columns:
        return sql

    selected_columns = columns[:max_columns]

    return (
        f"{match.group('prefix')}{', '.join(selected_columns)} "
        f"{match.group('from')}{match.group('table')}{match.group('rest')}"
    )

def detect_destructive_query_intent(query: str, settings: Settings) -> str | None:
    normalized_query = " ".join(query.lower().split())
    for keyword in _parse_csv_terms(settings.destructive_query_keywords):
        if re.search(rf"\b{re.escape(keyword)}\b", normalized_query, flags=re.IGNORECASE):
            return keyword
    return None

def detect_basic_ambiguity_reason(query: str, settings: Settings) -> str | None:
    stopwords = _parse_csv_terms(settings.ambiguity_query_stopwords)
    generic_terms = _parse_csv_terms(settings.ambiguity_generic_terms)
    modifier_terms = _parse_csv_terms(settings.ambiguity_modifier_terms)
    tokens = [
        token
        for token in re.findall(r"[a-z0-9_]+", query.lower())
        if token not in stopwords and not token.isdigit()
    ]
    if not tokens:
        return "The request does not identify which business entity to query."

    generic_tokens = [token for token in tokens if token in generic_terms]
    specific_tokens = [
        token
        for token in tokens
        if token not in generic_terms and token not in modifier_terms
    ]
    if generic_tokens and not specific_tokens:
        return (
            "The request uses generic terms like "
            f"{', '.join(sorted(set(generic_tokens)))} without naming the target entity."
        )
    return None

def _query_requests_all_columns(query: str) -> bool:
    return bool(
        re.search(
            r"\b(?:all columns|every column|full details|complete details|"
            r"full row|raw rows|select star)\b",
            query,
            flags=re.IGNORECASE,
        )
    )

def validate_sql_safety(
    sql: str,
    dialect: str,
) -> list[SqlWarning]:
    del dialect
    stripped = sql.strip()
    if not stripped:
        return [
            SqlWarning(
                code=WarningCode.SQL_EMPTY,
                message="Generated SQL is empty.",
            )
        ]

    statements = [statement for statement in sqlparse.split(stripped) if statement.strip()]
    if len(statements) > 1:
        return [
            SqlWarning(
                code=WarningCode.SQL_MULTI_STATEMENT,
                message="Generated SQL contains more than one statement.",
            )
        ]

    parsed_statements = sqlparse.parse(stripped)
    if not parsed_statements:
        return [
            SqlWarning(
                code=WarningCode.SQL_EMPTY,
                message="Generated SQL is empty.",
            )
        ]

    parsed = parsed_statements[0]
    destructive_keyword = _find_destructive_keyword(parsed)
    if destructive_keyword:
        return [
            SqlWarning(
                code=WarningCode.SQL_DESTRUCTIVE,
                message=f"Generated SQL contains destructive keyword: {destructive_keyword}.",
            )
        ]

    if not _is_select_statement(parsed):
        return [
            SqlWarning(
                code=WarningCode.SQL_NOT_SELECT,
                message="Generated SQL must be a SELECT statement.",
            )
        ]

    return []

def validate_tables_used(
    sql: str,
    tables_in_scope: list[str],
) -> tuple[list[str], list[SqlWarning]]:
    parsed_statements = sqlparse.parse(sql)
    if not parsed_statements:
        return [], []

    allowed_lookup = {
        _normalize_table_name(table).lower(): _normalize_table_name(table)
        for table in tables_in_scope
        if _normalize_table_name(table)
    }

    cte_names: set[str] = set()
    found_tables: list[str] = []
    for statement in parsed_statements:
        cte_names.update(_collect_cte_names(statement))
        found_tables.extend(_extract_table_names(statement))

    tables_used: list[str] = []
    unknown_tables: list[str] = []
    for table in found_tables:
        normalized = _normalize_table_name(table)
        if not normalized:
            continue

        lookup_key = normalized.lower()
        if lookup_key in allowed_lookup:
            allowed_name = allowed_lookup[lookup_key]
            if allowed_name not in tables_used:
                tables_used.append(allowed_name)
        elif lookup_key not in cte_names and normalized not in unknown_tables:
            unknown_tables.append(normalized)

    if unknown_tables:
        return tables_used, [
            SqlWarning(
                code=WarningCode.TABLE_OUT_OF_SCOPE,
                message=(
                    f"Unknown tables: {unknown_tables}. "
                    f"Allowed: {tables_in_scope}"
                ),
            )
        ]

    return tables_used, []

def validate_columns_used(
    sql: str,
    allowed_columns: dict[str, list[str]],
) -> list[SqlWarning]:
    normalized_allowed = {
        _normalize_table_name(table).lower(): {column.lower() for column in columns}
        for table, columns in allowed_columns.items()
        if _normalize_table_name(table) and columns
    }
    if not normalized_allowed:
        return []

    allowed_column_names = {
        column
        for columns in normalized_allowed.values()
        for column in columns
    }
    table_aliases = _extract_table_aliases(sql)
    unknown_columns: list[str] = []

    for qualifier, column in _extract_column_references(sql):
        column_key = column.lower()
        if column_key == "*":
            continue

        if qualifier:
            qualifier_key = _normalize_table_name(qualifier).lower()
            table_key = table_aliases.get(qualifier_key, qualifier_key)
            if table_key in normalized_allowed:
                if column_key not in normalized_allowed[table_key]:
                    unknown = f"{qualifier}.{column}"
                    if unknown not in unknown_columns:
                        unknown_columns.append(unknown)
                continue

        if column_key not in allowed_column_names and column not in unknown_columns:
            unknown_columns.append(column)

    if unknown_columns:
        return [
            SqlWarning(
                code=WarningCode.COLUMN_OUT_OF_SCOPE,
                message=(
                    f"Unknown columns: {unknown_columns}. "
                    f"Allowed columns are loaded for: {list(normalized_allowed)}"
                ),
            )
        ]

    return []

async def run_explain(sql: str, settings: Settings) -> list[SqlWarning]:
    schema_name = (settings.db_name or settings.db_central or "").strip()
    if not schema_name:
        return [
            SqlWarning(
                code=WarningCode.MYSQL_EXPLAIN_UNAVAILABLE,
                message="MySQL EXPLAIN unavailable because DB_NAME/DB_CENTRAL is not set.",
            )
        ]

    try:
        import aiomysql
    except ImportError:
        return [
            SqlWarning(
                code=WarningCode.MYSQL_EXPLAIN_UNAVAILABLE,
                message="MySQL EXPLAIN unavailable because aiomysql is not installed.",
            )
        ]

    connection = None
    try:
        connection = await aiomysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            db=schema_name,
            autocommit=True,
        )
    except Exception as exc:  # noqa: BLE001
        return [
            SqlWarning(
                code=WarningCode.MYSQL_EXPLAIN_UNAVAILABLE,
                message=f"MySQL EXPLAIN unavailable: {exc}",
            )
        ]

    try:
        async with connection.cursor() as cursor:
            await cursor.execute(f"EXPLAIN {sql}")
        return []
    except Exception as exc:  # noqa: BLE001
        return [
            SqlWarning(
                code=WarningCode.MYSQL_EXPLAIN_ERROR,
                message=f"MySQL EXPLAIN failed: {exc}",
            )
        ]
    finally:
        connection.close()

async def review_sql(
    sql: str,
    query: str,
    tables_in_scope: list[str],
    allowed_columns: dict[str, list[str]],
    settings: Settings,
) -> tuple[bool, list[str]]:
    known_columns_formatted = (
        "\n".join(
            f"- {table}: {', '.join(columns)}"
            for table, columns in allowed_columns.items()
        )
        if allowed_columns
        else "(none)"
    )
    prompt = f"""
You are a strict SQL reviewer for a MySQL database.
Review the SQL below against these rules:

1. Is it a single SELECT or WITH...SELECT? (no DML)
2. Does it only use these tables: {tables_in_scope}?
3. Does it actually answer: "{query}"?
4. Are the WHERE conditions sensible for the question?
5. Are all table.column references valid given the
   known columns: {known_columns_formatted}?

SQL to review:
{sql}

Output EXACTLY in this format — no other text:
VERDICT: PASS or FAIL
VIOLATIONS: <comma-separated list of rule numbers
             that failed, or "none" if PASS>
REASON: <one sentence explaining the verdict>
""".strip()
    client = get_model_client(
        settings=settings,
        model=settings.reasoning_model,
        default_timeout=15,
        role=LLMRole.REASONING.value,
    )
    response = await client.generate(
        prompt=prompt,
        max_tokens=settings.sql_subcall_max_tokens,
        temperature=0.0,
        enable_thinking=False,
        timeout=15,
    )
    if not response.text:
        return True, []

    verdict = ""
    violations_raw = ""
    reason = ""
    for raw_line in response.text.splitlines():
        line = raw_line.strip()
        if line.upper().startswith("VERDICT:"):
            verdict = line.split(":", 1)[1].strip()
        elif line.upper().startswith("VIOLATIONS:"):
            violations_raw = line.split(":", 1)[1].strip()
        elif line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()

    if not verdict or not violations_raw or not reason:
        return True, []

    if "FAIL" in verdict.upper():
        violations = [
            item.strip()
            for item in violations_raw.split(",")
            if item.strip() and item.strip().lower() != "none"
        ]
        return False, violations

    if "PASS" in verdict.upper():
        return True, []

    return True, []

async def _apply_review_gate(
    result: GenerateSqlSuccess,
    query: str,
    pool: asyncpg.Pool,
    settings: Settings,
    top_k: int,
) -> GenerateSqlSuccess:
    if not settings.governance_enabled:
        return result

    review_tables_in_scope = result.tables_used
    allowed_columns: dict[str, list[str]] = {}
    try:
        search_query = await query_rewriter.rewrite_search_query(query, pool, settings)
        retrieved = await retrieve.retrieve_groups(
            query=query,
            top_k=top_k,
            pool=pool,
            search_query=search_query,
        )
        review_tables_in_scope = _result_value(retrieved, "tables_in_scope")
        allowed_columns = await load_columns_for_tables(
            tables=review_tables_in_scope,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "Review gate context refresh failed for query %r: %s",
            query[:60],
            exc,
        )
        try:
            allowed_columns = await load_columns_for_tables(
                tables=result.tables_used,
                settings=settings,
            )
        except Exception:  # noqa: BLE001
            allowed_columns = {}

    passes, violations = await review_sql(
        sql=result.sql,
        query=query,
        tables_in_scope=review_tables_in_scope,
        allowed_columns=allowed_columns,
        settings=settings,
    )
    if passes:
        return result

    logger.info(
        "Review gate FAIL for query %r: violations=%s",
        query[:60],
        violations,
    )
    warning = SqlWarning(
        code=WarningCode.REVIEW_FAILED,
        message=(
            "Review gate flagged issues with rules: "
            f"{', '.join(violations) or 'unknown'}. "
            "SQL may still be correct — verify manually."
        ),
    )
    return result.model_copy(update={"warnings": [*result.warnings, warning]})

async def generate_sql(
    query: str,
    pool: asyncpg.Pool,
    settings: Settings,
    top_k: int | None = None,
    trace_callback: TraceCallback | None = None,
) -> GenerateSqlResponse:
    from nl2sql_service.agent.react_agent import run as react_run

    effective_top_k = top_k or settings.top_k
    cache_epoch: int | None = None
    query_embedding: list[float] | None = None
    search_query = query
    destructive_keyword = detect_destructive_query_intent(query, settings)
    if destructive_keyword:
        await _emit_trace(
            trace_callback,
            stage="sql_generation",
            status="failed",
            message="Destructive query intent rejected before SQL planning.",
            warning_codes=[WarningCode.SQL_DESTRUCTIVE.value],
            error_source="preflight_guardrail",
            details={"destructive_keyword": destructive_keyword},
        )
        return GenerateSqlRejected(
            warnings=[
                SqlWarning(
                    code=WarningCode.SQL_DESTRUCTIVE,
                    message=(
                        "The request asks for a destructive operation "
                        f"({destructive_keyword}), but only read-only SQL is allowed."
                    ),
                )
            ],
            attempt_count=0,
            react_trace=ReactTrace(
                steps=[],
                total_iterations=0,
                final_action=ReActAction.GIVE_UP,
            ),
        )

    ambiguity_reason = detect_basic_ambiguity_reason(query, settings)
    if ambiguity_reason:
        ambiguity_tables_in_scope: list[str] = []
        try:
            search_query = await query_rewriter.rewrite_search_query(query, pool, settings)
            ambiguity_retrieved = await retrieve.retrieve_groups(
                query=query,
                top_k=effective_top_k,
                pool=pool,
                search_query=search_query,
            )
            ambiguity_tables_in_scope = list(
                _result_value(ambiguity_retrieved, "tables_in_scope") or []
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "Ambiguity preflight retrieval failed for query %r: %s",
                query[:80],
                exc,
            )
        await _emit_trace(
            trace_callback,
            stage="sql_generation",
            status="needs_context",
            message="Basic ambiguity preflight requested clarification before SQL planning.",
            details={"reason": ambiguity_reason, "tables_in_scope": ambiguity_tables_in_scope},
        )
        return GenerateSqlClarification(
            question=(
                "Which table or business entity do you want to query for "
                f"'{query}'?"
            ),
            suggestions=_build_schema_derived_suggestions(ambiguity_tables_in_scope),
            original_query=query,
            failure_reason=ambiguity_reason,
            react_trace=ReactTrace(
                steps=[],
                total_iterations=0,
                final_action=ReActAction.REQUEST_CLARIFICATION,
            ),
        )

    await _emit_trace(
        trace_callback,
        stage="cache_lookup",
        status="started",
        message="Checking SQL cache before running the agent.",
        details={"endpoint": "generate-sql", "top_k": effective_top_k},
    )
    if settings.sql_cache_enabled:
        cached = sql_cache.get(query, effective_top_k)
        if cached:
            await _emit_trace(
                trace_callback,
                stage="cache_lookup",
                status="completed",
                message="SQL cache hit in memory.",
                details={"cache_source": CacheSource.MEMORY_EXACT.value},
            )
            return _generate_sql_response_from_dict(
                _with_cache_metadata(cached, CacheSource.MEMORY_EXACT)
            )

    vector_store = PgVectorStore(pool)
    deterministic_candidate = False
    deterministic_retrieved: Any | None = None
    deterministic_tables_in_scope: list[str] = []
    deterministic_matched_groups: list[str] = []

    await _emit_trace(
        trace_callback,
        stage="cache_lookup",
        status="completed",
        message="No reusable SQL cache entry found.",
        details={"cache_source": CacheSource.NONE.value},
    )

    obvious_deterministic_result: GenerateSqlSuccess | None = None
    try:
        search_query = await query_rewriter.rewrite_search_query(query, pool, settings)
        deterministic_retrieved = await retrieve.retrieve_groups(
            query=query,
            top_k=effective_top_k,
            pool=pool,
            search_query=search_query,
        )
        obvious_tables_in_scope = list(
            _result_value(deterministic_retrieved, "tables_in_scope") or []
        )
        if len(obvious_tables_in_scope) == 1:
            obvious_columns = await load_columns_for_tables(
                tables=obvious_tables_in_scope,
                settings=settings,
            )
            built = build_deterministic_sql(
                query=query,
                allowed_columns=obvious_columns,
                top_k=effective_top_k,
                settings=settings,
            )
            if built is not None:
                sql, tables_used = built
                warnings = validate_sql_safety(sql, settings.sql_dialect)
                validated_tables, table_warnings = validate_tables_used(sql, tables_used)
                warnings.extend(table_warnings)
                warnings.extend(validate_columns_used(sql, obvious_columns))
                if not _has_blocking_warnings(warnings):
                    explain_warnings = await run_explain(sql, settings)
                    if not _has_blocking_warnings(explain_warnings):
                        obvious_deterministic_result = GenerateSqlSuccess(
                            sql=sql,
                            warnings=[
                                warning
                                for warning in explain_warnings
                                if warning.code == WarningCode.MYSQL_EXPLAIN_UNAVAILABLE
                            ],
                            tables_used=validated_tables,
                            matched_groups=[f"deterministic_{tables_used[0]}"],
                            attempt_count=0,
                            react_trace=None,
                        )
    except Exception as exc:  # noqa: BLE001
        logger.info("Obvious deterministic generation precheck failed for query %r: %s", query[:80], exc)

    if obvious_deterministic_result is not None:
        deterministic_result = obvious_deterministic_result
        await _emit_trace(
            trace_callback,
            stage="sql_generation",
            status="completed",
            message="Deterministic SQL rule generated a validated query.",
            details={
                "matched_groups": deterministic_result.matched_groups,
                "tables_used": deterministic_result.tables_used,
                "sql_preview": deterministic_result.sql[:500],
            },
        )
        if settings.sql_cache_enabled and deterministic_result.status == "ok":
            payload = deterministic_result.model_dump(mode="json")
            payload["cache_hit"] = False
            payload["cache_source"] = CacheSource.NONE.value
            sql_cache.set(query, effective_top_k, payload)
            try:
                await db.upsert_query_cache_entry(
                    pool,
                    endpoint="generate-sql",
                    query_text=query,
                    top_k=effective_top_k,
                    response_json=payload,
                    query_embedding=None,
                    cache_epoch=cache_epoch or await db.get_query_cache_epoch(pool),
                )
            except Exception:
                logger.exception("Failed to persist generate-sql cache entry")
        return deterministic_result

    try:
        query_embedding = await _load_query_embedding(query)
    except Exception:
        query_embedding = None

    if query_embedding is not None:
        try:
            if deterministic_retrieved is None:
                search_query = await query_rewriter.rewrite_search_query(query, pool, settings)
                deterministic_retrieved = await retrieve.retrieve_groups(
                    query=query,
                    top_k=effective_top_k,
                    pool=pool,
                    search_query=search_query,
                )
            deterministic_tables_in_scope = list(
                _result_value(deterministic_retrieved, "tables_in_scope") or []
            )
            deterministic_matched_groups = list(
                _result_value(deterministic_retrieved, "matched_groups") or []
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("Deterministic schema context retrieval failed for query %r: %s", query[:80], exc)
        deterministic_candidate = await is_deterministic_generation_candidate(
            query=query,
            query_embedding=query_embedding,
            settings=settings,
            vector_store=vector_store,
            known_schema_tables=deterministic_tables_in_scope,
            matched_groups=deterministic_matched_groups,
        )

    if settings.sql_cache_enabled:
        try:
            if query_embedding is None:
                raise ValueError("query embedding unavailable")
            sem_cached = semantic_sql_cache.get_semantic(
                query_embedding,
                effective_top_k,
                threshold=settings.sql_cache_semantic_threshold,
            )
            if sem_cached:
                await _emit_trace(
                    trace_callback,
                    stage="cache_lookup",
                    status="completed",
                    message="Semantic SQL cache hit in memory.",
                    details={"cache_source": CacheSource.MEMORY_SEMANTIC.value},
                )
                return _generate_sql_response_from_dict(
                    _with_cache_metadata(sem_cached, CacheSource.MEMORY_SEMANTIC)
                )
        except Exception:
            pass

    if settings.sql_cache_enabled:
        try:
            cache_epoch = await db.get_query_cache_epoch(pool)
            db_exact = await db.get_query_cache_exact(
                pool,
                endpoint="generate-sql",
                query_text=query,
                top_k=effective_top_k,
                cache_epoch=cache_epoch,
            )
            if db_exact:
                warmed = _with_cache_metadata(db_exact, CacheSource.DB_EXACT)
                sql_cache.set(query, effective_top_k, db_exact)
                await _emit_trace(
                    trace_callback,
                    stage="cache_lookup",
                    status="completed",
                    message="SQL cache hit in PostgreSQL.",
                    details={"cache_source": CacheSource.DB_EXACT.value},
                )
                if query_embedding is not None:
                    semantic_sql_cache.set(query, effective_top_k, db_exact, embedding=query_embedding)
                return _generate_sql_response_from_dict(warmed)

            if query_embedding is not None:
                db_semantic = await db.get_query_cache_semantic(
                    pool,
                    endpoint="generate-sql",
                    query_embedding=query_embedding,
                    top_k=effective_top_k,
                    cache_epoch=cache_epoch,
                    min_similarity=settings.sql_cache_semantic_threshold,
                )
                if db_semantic:
                    sql_cache.set(query, effective_top_k, db_semantic)
                    semantic_sql_cache.set(
                        query,
                        effective_top_k,
                        db_semantic,
                        embedding=query_embedding,
                    )
                    await _emit_trace(
                        trace_callback,
                        stage="cache_lookup",
                        status="completed",
                        message="Semantic SQL cache hit in PostgreSQL.",
                        details={"cache_source": CacheSource.DB_SEMANTIC.value},
                    )
                    return _generate_sql_response_from_dict(
                        _with_cache_metadata(db_semantic, CacheSource.DB_SEMANTIC)
                    )
        except Exception:
            logger.exception("Failed DB SQL cache lookup")

    deterministic_result = await _try_deterministic_generation(
        query=query,
        pool=pool,
        settings=settings,
        top_k=effective_top_k,
        query_embedding=query_embedding,
        vector_store=vector_store,
        deterministic_candidate=deterministic_candidate,
        retrieved=deterministic_retrieved,
    )
    if deterministic_result is not None:
        await _emit_trace(
            trace_callback,
            stage="sql_generation",
            status="completed",
            message="Deterministic SQL rule generated a validated query.",
            details={
                "matched_groups": deterministic_result.matched_groups,
                "tables_used": deterministic_result.tables_used,
                "sql_preview": deterministic_result.sql[:500],
            },
        )
        if settings.sql_cache_enabled and deterministic_result.status == "ok":
            payload = deterministic_result.model_dump(mode="json")
            payload["cache_hit"] = False
            payload["cache_source"] = CacheSource.NONE.value
            sql_cache.set(query, effective_top_k, payload)
            try:
                if query_embedding is not None:
                    semantic_sql_cache.set(
                        query,
                        effective_top_k,
                        payload,
                        embedding=query_embedding,
                    )
            except Exception:
                pass
            try:
                await db.upsert_query_cache_entry(
                    pool,
                    endpoint="generate-sql",
                    query_text=query,
                    top_k=effective_top_k,
                    response_json=payload,
                    query_embedding=query_embedding,
                    cache_epoch=cache_epoch or await db.get_query_cache_epoch(pool),
                )
            except Exception:
                logger.exception("Failed to persist generate-sql cache entry")
        return deterministic_result

    try:
        result = await asyncio.wait_for(
            react_run(
                query=query,
                pool=pool,
                settings=settings,
                top_k=top_k,
                trace_callback=trace_callback,
            ),
            timeout=settings.sql_generation_timeout,
        )
    except asyncio.TimeoutError:
        await _emit_trace(
            trace_callback,
            stage="sql_generation",
            status="failed",
            message=(
                "SQL generation exceeded the service time budget "
                f"of {settings.sql_generation_timeout}s."
            ),
            warning_codes=[WarningCode.REQUEST_TIMEOUT.value],
            error_source="service_timeout",
        )
        result = GenerateSqlRejected(
            warnings=[
                SqlWarning(
                    code=WarningCode.REQUEST_TIMEOUT,
                    message=(
                        "SQL generation exceeded the service time budget "
                        f"of {settings.sql_generation_timeout}s."
                    ),
                )
            ],
            attempt_count=0,
            react_trace=None,
        )
    if result.status == "ok" and settings.governance_enabled:
        await _emit_trace(
            trace_callback,
            stage="review_gate",
            status="started",
            message="Running governance review for generated SQL.",
            details={"tables_used": result.tables_used},
        )
        result = await _apply_review_gate(
            result=result,
            query=query,
            pool=pool,
            settings=settings,
            top_k=effective_top_k,
        )
        await _emit_trace(
            trace_callback,
            stage="review_gate",
            status="completed" if not any(w.code == WarningCode.REVIEW_FAILED for w in result.warnings) else "warning",
            message="Governance review completed.",
            warning_codes=[warning.code.value for warning in result.warnings],
        )
    if settings.sql_cache_enabled and result.status == "ok":
        payload = result.model_dump(mode="json")
        payload["cache_hit"] = False
        payload["cache_source"] = CacheSource.NONE.value
        sql_cache.set(query, effective_top_k, payload)
        try:
            if query_embedding is None:
                query_embedding = await _load_query_embedding(query)
            if query_embedding is not None:
                semantic_sql_cache.set(query, effective_top_k, payload, embedding=query_embedding)
        except Exception:
            pass
        try:
            await db.upsert_query_cache_entry(
                pool,
                endpoint="generate-sql",
                query_text=query,
                top_k=effective_top_k,
                response_json=payload,
                query_embedding=query_embedding,
                cache_epoch=cache_epoch or await db.get_query_cache_epoch(pool),
            )
            await _emit_trace(
                trace_callback,
                stage="cache_write",
                status="completed",
                message="Stored successful SQL generation in cache.",
                details={"endpoint": "generate-sql"},
            )
        except Exception:
            logger.exception("Failed to persist generate-sql cache entry")
            await _emit_trace(
                trace_callback,
                stage="cache_write",
                status="warning",
                message="Failed to persist SQL generation cache entry.",
                error_source="cache_write",
            )
    return result

async def _try_deterministic_generation(
    query: str,
    pool: asyncpg.Pool,
    settings: Settings,
    top_k: int,
    query_embedding: list[float] | None,
    vector_store: VectorStore,
    deterministic_candidate: bool,
    retrieved: Any | None,
) -> GenerateSqlSuccess | None:
    del query_embedding, vector_store
    if not deterministic_candidate:
        return None

    try:
        if retrieved is None:
            search_query = await query_rewriter.rewrite_search_query(query, pool, settings)
            retrieved = await retrieve.retrieve_groups(
                query=query,
                top_k=top_k,
                pool=pool,
                search_query=search_query,
            )
        tables_in_scope = _result_value(retrieved, "tables_in_scope")
    except Exception as exc:  # noqa: BLE001
        logger.info("Deterministic retrieval failed for query %r: %s", query[:80], exc)
        return None

    if not tables_in_scope:
        return None

    allowed_columns = await load_columns_for_tables(
        tables=tables_in_scope,
        settings=settings,
    )
    built = build_deterministic_sql(
        query=query,
        allowed_columns=allowed_columns,
        top_k=top_k,
    )
    if built is None:
        return None

    sql, tables_used = built
    warnings = validate_sql_safety(sql, settings.sql_dialect)
    if warnings:
        return None

    validated_tables, table_warnings = validate_tables_used(sql, tables_used)
    warnings.extend(table_warnings)
    warnings.extend(validate_columns_used(sql, allowed_columns))
    if _has_blocking_warnings(warnings):
        logger.info(
            "Deterministic NL2SQL candidate failed validation for query %r: %s",
            query[:80],
            [warning.message for warning in warnings],
        )
        return None

    explain_warnings = await run_explain(sql, settings)
    if _has_blocking_warnings(explain_warnings):
        logger.info(
            "Deterministic NL2SQL candidate failed EXPLAIN for query %r: %s",
            query[:80],
            [warning.message for warning in explain_warnings],
        )
        return None

    return GenerateSqlSuccess(
        sql=sql,
        warnings=[
            warning
            for warning in explain_warnings
            if warning.code == WarningCode.MYSQL_EXPLAIN_UNAVAILABLE
        ],
        tables_used=validated_tables,
        matched_groups=[f"deterministic_{tables_used[0]}"],
        attempt_count=0,
        react_trace=None,
    )

def _generate_sql_response_from_dict(payload: dict[str, Any]) -> GenerateSqlResponse:
    status = payload.get("status")
    if status == "ok":
        return GenerateSqlSuccess(**payload)
    if status == "rejected":
        return GenerateSqlRejected(**payload)
    if status == "clarification_needed":
        return GenerateSqlClarification(**payload)
    raise ValueError(f"Unknown SQL generation status in cache: {status}")

def _result_value(result: Any, field: str) -> Any:
    if isinstance(result, dict):
        return result[field]
    return getattr(result, field)

def _find_destructive_keyword(statement: Statement) -> str | None:
    for token in statement.flatten():
        if token.is_whitespace or _is_comment_or_string(token):
            continue
        value = token.value.strip().upper()
        if value in _DESTRUCTIVE_KEYWORDS:
            return value
    return None

def _is_comment_or_string(token: Token) -> bool:
    return (
        isinstance(token, Comment)
        or token.ttype in T.Comment
        or token.ttype in T.String
        or token.ttype in T.Literal.String
    )

def _is_select_statement(statement: Statement) -> bool:
    if statement.get_type() == "SELECT":
        return True

    first_token = _first_meaningful_token(statement)
    if first_token is None:
        return False

    first_value = first_token.normalized.upper()
    if first_value == "SELECT":
        return True
    if first_value == "WITH" and re.search(r"\bSELECT\b", statement.value, re.IGNORECASE):
        return True

    return False

def _first_meaningful_token(token_list: TokenList) -> Token | None:
    for token in token_list.tokens:
        if token.is_whitespace or _is_comment_or_string(token):
            continue
        return token
    return None

def _collect_cte_names(statement: Statement) -> set[str]:
    cte_names: set[str] = set()
    seen_with = False

    for token in statement.tokens:
        if token.is_whitespace or _is_comment_or_string(token):
            continue

        normalized = token.normalized.upper()
        if not seen_with:
            if normalized == "WITH":
                seen_with = True
                continue
            return cte_names

        if normalized == "SELECT":
            break

        if isinstance(token, IdentifierList):
            for identifier in token.get_identifiers():
                name = _identifier_name(identifier)
                if name:
                    cte_names.add(name.lower())
        elif isinstance(token, Identifier):
            name = _identifier_name(token)
            if name:
                cte_names.add(name.lower())

    return cte_names

def _extract_table_names(statement: Statement) -> list[str]:
    return _extract_table_names_from_tokenlist(statement)

def _extract_table_names_from_tokenlist(token_list: TokenList) -> list[str]:
    tables: list[str] = []
    tokens = list(token_list.tokens)
    index = 0

    while index < len(tokens):
        token = tokens[index]

        if token.is_group:
            tables.extend(_extract_table_names_from_tokenlist(token))

        if _is_from_or_join(token):
            index += 1
            while index < len(tokens):
                candidate = tokens[index]
                if candidate.is_whitespace or _is_comment_or_string(candidate):
                    index += 1
                    continue
                if _is_clause_end(candidate):
                    break

                tables.extend(_tables_from_candidate(candidate))
                break

        index += 1

    return tables

def _tables_from_candidate(candidate: Token) -> list[str]:
    if isinstance(candidate, IdentifierList):
        tables: list[str] = []
        for identifier in candidate.get_identifiers():
            tables.extend(_tables_from_identifier(identifier))
        return tables

    if isinstance(candidate, Identifier):
        return _tables_from_identifier(candidate)

    if isinstance(candidate, Parenthesis):
        return _extract_table_names_from_tokenlist(candidate)

    if candidate.ttype in T.Name or candidate.ttype in T.Keyword:
        return [candidate.value]

    if candidate.is_group:
        return _extract_table_names_from_tokenlist(candidate)

    return []

def _tables_from_identifier(identifier: Identifier) -> list[str]:
    for token in identifier.tokens:
        if isinstance(token, Parenthesis):
            return _extract_table_names_from_tokenlist(token)

    name = _identifier_name(identifier)
    return [name] if name else []

def _identifier_name(identifier: Identifier) -> str:
    name = identifier.get_real_name() or identifier.get_name() or identifier.value
    return _normalize_table_name(name)

def _is_from_or_join(token: Token) -> bool:
    normalized = token.normalized.upper()
    return normalized == "FROM" or "JOIN" in normalized.split()

def _is_clause_end(token: Token) -> bool:
    normalized = token.normalized.upper()
    return normalized in _CLAUSE_END_KEYWORDS

def _normalize_table_name(name: str) -> str:
    cleaned = name.strip().strip("`\"'[]")
    if not cleaned:
        return ""
    if "." in cleaned:
        cleaned = cleaned.split(".")[-1]
    return cleaned.strip().strip("`\"'[]")

def _extract_table_aliases(sql: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for statement in sqlparse.parse(sql):
        aliases.update(_extract_table_aliases_from_tokenlist(statement))
    return aliases

def _extract_table_aliases_from_tokenlist(token_list: TokenList) -> dict[str, str]:
    aliases: dict[str, str] = {}
    tokens = list(token_list.tokens)
    index = 0

    while index < len(tokens):
        token = tokens[index]
        if token.is_group:
            aliases.update(_extract_table_aliases_from_tokenlist(token))

        if _is_from_or_join(token):
            index += 1
            while index < len(tokens):
                candidate = tokens[index]
                if candidate.is_whitespace or _is_comment_or_string(candidate):
                    index += 1
                    continue
                if _is_clause_end(candidate):
                    break

                aliases.update(_table_aliases_from_candidate(candidate))
                break

        index += 1

    return aliases

def _table_aliases_from_candidate(candidate: Token) -> dict[str, str]:
    if isinstance(candidate, IdentifierList):
        aliases: dict[str, str] = {}
        for identifier in candidate.get_identifiers():
            aliases.update(_table_aliases_from_identifier(identifier))
        return aliases

    if isinstance(candidate, Identifier):
        return _table_aliases_from_identifier(candidate)

    if candidate.ttype in T.Name or candidate.ttype in T.Keyword:
        table = _normalize_table_name(candidate.value).lower()
        return {table: table} if table else {}

    if candidate.is_group:
        return _extract_table_aliases_from_tokenlist(candidate)

    return {}

def _table_aliases_from_identifier(identifier: Identifier) -> dict[str, str]:
    for token in identifier.tokens:
        if isinstance(token, Parenthesis):
            return _extract_table_aliases_from_tokenlist(token)

    table = _normalize_table_name(identifier.get_real_name() or "")
    if not table:
        return {}

    table_key = table.lower()
    alias = _normalize_table_name(identifier.get_alias() or table).lower()
    aliases = {table_key: table_key}
    if alias:
        aliases[alias] = table_key
    return aliases

def _extract_column_references(sql: str) -> list[tuple[str | None, str]]:
    references: list[tuple[str | None, str]] = []
    parsed_statements = sqlparse.parse(sql)
    cte_names: set[str] = set()
    table_names: set[str] = set()
    table_aliases: set[str] = set()

    for statement in parsed_statements:
        cte_names.update(_collect_cte_names(statement))
        table_names.update(
            _normalize_table_name(table).lower()
            for table in _extract_table_names(statement)
            if _normalize_table_name(table)
        )

    alias_lookup = _extract_table_aliases(sql)
    table_aliases.update(alias_lookup)
    skip_names = cte_names | table_names | table_aliases

    for statement in parsed_statements:
        tokens = [token for token in statement.flatten() if not token.is_whitespace]
        for index, token in enumerate(tokens):
            if _should_skip_column_token(token):
                continue

            name = _normalize_table_name(token.value)
            if not name or not _SQL_NAME_RE.match(name):
                continue

            upper_name = name.upper()
            if upper_name in _COLUMN_NAME_SKIP_KEYWORDS or name.lower() in skip_names:
                continue

            previous_token = _previous_meaningful(tokens, index)
            next_token = _next_meaningful(tokens, index)
            previous_value = previous_token.value if previous_token else ""
            next_value = next_token.value if next_token else ""

            if next_value == "(" or next_value == ".":
                continue
            if previous_token and previous_token.normalized.upper() in {
                "AS",
                "FROM",
                "JOIN",
                "INTO",
                "UPDATE",
            }:
                continue

            qualifier: str | None = None
            if previous_value == ".":
                qualifier_token = _previous_meaningful(tokens, index - 1)
                if qualifier_token is not None:
                    qualifier = _normalize_table_name(qualifier_token.value)

            reference = (qualifier, name)
            if reference not in references:
                references.append(reference)

    return references

def _should_skip_column_token(token: Token) -> bool:
    if token.is_whitespace or _is_comment_or_string(token):
        return True
    if token.ttype in T.Literal.Number:
        return True
    if token.ttype in T.Keyword:
        return True
    if token.ttype in T.Punctuation or token.ttype in T.Operator:
        return True
    if token.ttype in T.Wildcard:
        return True
    return False

def _previous_meaningful(tokens: list[Token], index: int) -> Token | None:
    cursor = index - 1
    while cursor >= 0:
        token = tokens[cursor]
        if not token.is_whitespace and not _is_comment_or_string(token):
            return token
        cursor -= 1
    return None

def _next_meaningful(tokens: list[Token], index: int) -> Token | None:
    cursor = index + 1
    while cursor < len(tokens):
        token = tokens[cursor]
        if not token.is_whitespace and not _is_comment_or_string(token):
            return token
        cursor += 1
    return None
