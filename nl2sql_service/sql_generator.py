from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import asyncpg
import sqlparse
from sqlparse import tokens as T
from sqlparse.sql import Comment, Identifier, IdentifierList, Parenthesis, Statement, Token, TokenList

from nl2sql_service import query_rewriter, retrieve
from nl2sql_service.cache import sql_cache
from nl2sql_service.column_loader import load_columns_for_tables
from nl2sql_service.config import Settings, settings as default_settings
from nl2sql_service.model_client import get_model_client
from nl2sql_service.models import (
    GenerateSqlClarification,
    GenerateSqlRejected,
    GenerateSqlResponse,
    GenerateSqlSuccess,
    SqlWarning,
    WarningCode,
)
from nl2sql_service.rulebook import build_governance_block, get_config

logger = logging.getLogger(__name__)

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
_PAYMENT_QUERY_RE = re.compile(r"\bpayments?\b", re.IGNORECASE)
_COUNT_RE = re.compile(r"\b(\d{1,3})\b")
_FINANCIAL_QUERY_TERMS = (
    "amount",
    "balance",
    "billing",
    "invoice",
    "invoices",
    "paid",
    "payment",
    "payments",
    "receipt",
    "revenue",
    "total",
)
_AUDIT_QUERY_TERMS = (
    "audit",
    "created by",
    "creator",
    "last updated",
    "modified",
    "modified by",
    "updated",
    "updated by",
)
_INQUIRY_QUERY_TERMS = (
    "enquiries",
    "enquiry",
    "inquiries",
    "inquiry",
    "lead",
    "leads",
    "prospect",
    "prospects",
)
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


def _quote_identifier(identifier: str) -> str:
    if _SQL_NAME_RE.match(identifier):
        return identifier
    return f"`{identifier.replace('`', '``')}`"


def _query_requests_recent_payment(query: str) -> bool:
    query_lower = query.lower()
    return bool(_PAYMENT_QUERY_RE.search(query_lower)) and any(
        term in query_lower for term in _RECENT_QUERY_TERMS
    )


def _deterministic_limit(query: str, top_k: int) -> int:
    match = _COUNT_RE.search(query)
    if match:
        return max(1, min(int(match.group(1)), 50))
    if re.search(r"\bpayment\b", query, flags=re.IGNORECASE) and not re.search(
        r"\bpayments\b",
        query,
        flags=re.IGNORECASE,
    ):
        return 1
    return max(1, min(top_k, 50))


def _best_recent_order_columns(columns: list[str]) -> list[str]:
    lookup = {column.lower(): column for column in columns}
    ordered: list[str] = []
    for preferred in (
        "date",
        "created_at",
        "modified_at",
        "id",
    ):
        column = lookup.get(preferred)
        if column:
            ordered.append(column)
    return ordered


def _select_payment_columns(query: str, columns: list[str], max_columns: int = 8) -> list[str]:
    del query
    lookup = {column.lower(): column for column in columns}
    selected: list[str] = []
    for preferred in (
        "id",
        "invoice_id",
        "date",
        "amount",
        "actual_amount",
        "calculated_amount",
        "receipt",
        "pay_mode_text",
        "payment_mode",
        "pay_mode",
        "txn_number",
        "tracking_id",
        "bank_ref_no",
        "created_at",
    ):
        column = lookup.get(preferred)
        if column and column not in selected:
            selected.append(column)
        if len(selected) >= max_columns:
            break
    return selected or columns[:max_columns]


def _has_blocking_warnings(warnings: list[SqlWarning]) -> bool:
    return any(warning.code != WarningCode.MYSQL_EXPLAIN_UNAVAILABLE for warning in warnings)


def build_deterministic_sql(
    query: str,
    allowed_columns: dict[str, list[str]],
    top_k: int,
) -> tuple[str, list[str]] | None:
    """Return validated-template SQL for high-confidence simple intents."""
    if not _query_requests_recent_payment(query):
        return None

    payment_columns = allowed_columns.get("payment") or allowed_columns.get("Payment")
    if not payment_columns:
        return None

    order_columns = _best_recent_order_columns(payment_columns)
    if not order_columns:
        return None

    selected_columns = _select_payment_columns(
        query=query,
        columns=payment_columns,
        max_columns=8,
    )
    for required in order_columns:
        if required not in selected_columns and len(selected_columns) < 8:
            selected_columns.append(required)
    if not selected_columns:
        return None

    limit = _deterministic_limit(query, top_k)
    select_list = ", ".join(_quote_identifier(column) for column in selected_columns)
    order_by = ", ".join(f"{_quote_identifier(column)} DESC" for column in order_columns)
    sql = f"SELECT {select_list} FROM payment ORDER BY {order_by} LIMIT {limit}"
    return sql, ["payment"]


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
) -> tuple[str | None, list[SqlWarning]]:
    client = get_model_client(
        settings=settings,
        model=settings.llm_model,
        default_timeout=settings.llm_timeout,
    )
    response = await client.generate(
        prompt=prompt,
        temperature=0.0,
        timeout=settings.llm_timeout,
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
    """Replace simple single-table SELECT * with a focused column list."""
    if _query_requests_all_columns(query):
        return sql

    match = _SIMPLE_SELECT_STAR_RE.match(sql.strip())
    if not match:
        return sql

    table_name = _normalize_table_name(match.group("table"))
    columns = allowed_columns.get(table_name.lower())
    if not columns:
        return sql

    selected_columns = _select_relevant_columns(query, columns, max_columns)
    if not selected_columns:
        return sql

    return (
        f"{match.group('prefix')}{', '.join(selected_columns)} "
        f"{match.group('from')}{match.group('table')}{match.group('rest')}"
    )


def _select_relevant_columns(
    query: str,
    columns: list[str],
    max_columns: int,
) -> list[str]:
    indexes = select_relevant_column_indexes(query, columns, max_columns)
    return [columns[index] for index in indexes]


def select_relevant_column_indexes(
    query: str,
    columns: list[str],
    max_columns: int = 8,
) -> list[int]:
    query_lower = query.lower()
    recent_query = any(term in query_lower for term in _RECENT_QUERY_TERMS)
    financial_query = any(term in query_lower for term in _FINANCIAL_QUERY_TERMS)
    audit_query = any(term in query_lower for term in _AUDIT_QUERY_TERMS)
    inquiry_query = any(term in query_lower for term in _INQUIRY_QUERY_TERMS)
    scored: list[tuple[int, int]] = []
    for index, column in enumerate(columns):
        column_lower = column.lower()
        score = _score_column_for_query(
            column_lower=column_lower,
            query_lower=query_lower,
            recent_query=recent_query,
            financial_query=financial_query,
            audit_query=audit_query,
            inquiry_query=inquiry_query,
        )
        if score > 0:
            scored.append((-score, index))

    selected = [index for _, index in sorted(scored)[:max_columns]]
    if selected:
        return selected

    fallback_indexes = [
        index
        for index, column in enumerate(columns)
        if not _is_low_signal_column(column.lower(), financial_query, audit_query)
    ]
    return (fallback_indexes or list(range(len(columns))))[:max_columns]


def _score_column_for_query(
    column_lower: str,
    query_lower: str,
    recent_query: bool,
    financial_query: bool,
    audit_query: bool,
    inquiry_query: bool,
) -> int:
    score = 0
    mentioned = _query_mentions_column(query_lower, column_lower)
    if mentioned:
        score += 220

    if column_lower == "id":
        score += 130
    elif column_lower == "invoice_id":
        score += 120 if financial_query else 60
    elif column_lower == "contact_id":
        score += 95 if inquiry_query else 65
    elif column_lower.endswith("_id"):
        score += 45

    if column_lower == "created_at":
        score += 110 if recent_query else 65
    elif column_lower in {"date", "doi", "doc"}:
        score += 95 if recent_query else 55
    elif column_lower == "allocation_date":
        score += 100 if inquiry_query and recent_query else 65
    elif any(fragment in column_lower for fragment in ("date", "time")):
        score += 70 if recent_query else 40

    if inquiry_query and column_lower in {
        "type",
        "source",
        "heard_from",
        "primary_heard_from",
        "primary_source",
        "converted",
        "allocation_date",
        "created_at",
        "doi",
        "doc",
    }:
        score += 75

    if any(fragment in column_lower for fragment in ("amount", "total")):
        score += 95 if financial_query else 20
    if column_lower == "balance":
        score += 90 if financial_query else -120
    if any(
        fragment in column_lower
        for fragment in ("receipt", "pay_mode", "payment_mode", "method")
    ):
        score += 80 if financial_query else 30
    if any(fragment in column_lower for fragment in ("reference", "txn", "tracking")):
        score += 55
    if "status" in column_lower or column_lower in {"converted", "active", "park"}:
        status_query = any(
            term in query_lower
            for term in ("active", "converted", "open", "status")
        )
        score += 70 if status_query else 45
    if "source" in column_lower or column_lower == "heard_from":
        score += 70 if inquiry_query or "source" in query_lower else 35

    if _is_audit_column(column_lower) and not (audit_query or mentioned):
        score -= 120

    return score


def _query_mentions_column(query_lower: str, column_lower: str) -> bool:
    return column_lower in query_lower or column_lower.replace("_", " ") in query_lower


def _query_requests_all_columns(query: str) -> bool:
    return bool(
        re.search(
            r"\b(?:all columns|every column|full details|complete details|"
            r"full row|raw rows|select star)\b",
            query,
            flags=re.IGNORECASE,
        )
    )


def _is_audit_column(column_lower: str) -> bool:
    return column_lower in {
        "created_by",
        "last_updated",
        "modified_at",
        "modified_by",
        "updated_at",
        "updated_by",
    }


def _is_low_signal_column(
    column_lower: str,
    financial_query: bool,
    audit_query: bool,
) -> bool:
    if column_lower == "balance" and not financial_query:
        return True
    if _is_audit_column(column_lower) and not audit_query:
        return True
    return False


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
    )
    response = await client.generate(
        prompt=prompt,
        max_tokens=150,
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
) -> GenerateSqlResponse:
    from nl2sql_service.react_agent import run as react_run

    effective_top_k = top_k or settings.top_k
    if settings.sql_cache_enabled:
        cached = sql_cache.get(query, effective_top_k)
        if cached:
            cached["cache_hit"] = True
            return _generate_sql_response_from_dict(cached)

    deterministic_result = await _try_deterministic_generation(
        query=query,
        settings=settings,
        top_k=effective_top_k,
    )
    if deterministic_result is not None:
        if settings.sql_cache_enabled and deterministic_result.status == "ok":
            payload = deterministic_result.model_dump(mode="json")
            payload["cache_hit"] = False
            sql_cache.set(query, effective_top_k, payload)
        return deterministic_result

    try:
        result = await asyncio.wait_for(
            react_run(
                query=query,
                pool=pool,
                settings=settings,
                top_k=top_k,
            ),
            timeout=settings.sql_generation_timeout,
        )
    except asyncio.TimeoutError:
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
        result = await _apply_review_gate(
            result=result,
            query=query,
            pool=pool,
            settings=settings,
            top_k=effective_top_k,
        )
    if settings.sql_cache_enabled and result.status == "ok":
        payload = result.model_dump(mode="json")
        payload["cache_hit"] = False
        sql_cache.set(query, effective_top_k, payload)
    return result


async def _try_deterministic_generation(
    query: str,
    settings: Settings,
    top_k: int,
) -> GenerateSqlSuccess | None:
    allowed_columns = await load_columns_for_tables(
        tables=["payment"],
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
        matched_groups=["deterministic_payment"],
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
