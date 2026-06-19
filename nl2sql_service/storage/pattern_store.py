from __future__ import annotations

import json
import logging
import re
from typing import Any

import asyncpg
import sqlparse

logger = logging.getLogger(__name__)


_JOIN_CLAUSE_RE = re.compile(
    r"""
    (?:(LEFT|RIGHT|INNER|CROSS)(?:\s+OUTER)?\s+)?
    JOIN\s+([`"\w.]+)
    (?:\s+(?:AS\s+)?([`"\w]+))?
    \s+ON\s+
    (.*?)
    (?=
        \b(?:LEFT|RIGHT|INNER|CROSS|FULL)?\s*(?:OUTER\s+)?JOIN\b
        |\bWHERE\b
        |\bGROUP\s+BY\b
        |\bORDER\s+BY\b
        |\bHAVING\b
        |\bLIMIT\b
        |$
    )
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)
_TABLE_REF_RE = re.compile(
    r"""
    \b(?:FROM|JOIN)\s+([`"\w.]+)
    (?:\s+(?:AS\s+)?([`"\w]+))?
    """,
    re.IGNORECASE | re.VERBOSE,
)
_JOIN_CONDITION_RE = re.compile(
    r"([`\"\w]+(?:\.[`\"\w]+){1,2})\s*=\s*([`\"\w]+(?:\.[`\"\w]+){1,2})",
    re.IGNORECASE,
)
_SQL_BOUNDARY_WORDS = {
    "ON",
    "WHERE",
    "JOIN",
    "LEFT",
    "RIGHT",
    "INNER",
    "CROSS",
    "FULL",
    "GROUP",
    "ORDER",
    "HAVING",
    "LIMIT",
}


def extract_join_conditions(sql: str) -> list[dict]:
    try:
        statements = sqlparse.parse(sql or "")
        if not statements:
            return []
        parsed_sql = " ".join(str(statement) for statement in statements)
        alias_map = _extract_alias_map(parsed_sql)

        joins: list[dict] = []
        for join_match in _JOIN_CLAUSE_RE.finditer(parsed_sql):
            join_type = (join_match.group(1) or "INNER").upper()
            joined_table = _resolve_joined_table(join_match, alias_map)
            on_clause = join_match.group(4)
            for condition_match in _JOIN_CONDITION_RE.finditer(on_clause):
                left = _parse_column_ref(condition_match.group(1), alias_map)
                right = _parse_column_ref(condition_match.group(2), alias_map)
                if left is None or right is None:
                    continue
                if left[0] == joined_table and right[0] != joined_table:
                    left, right = right, left
                joins.append(
                    {
                        "left_table": left[0],
                        "left_column": left[1],
                        "right_table": right[0],
                        "right_column": right[1],
                        "join_type": join_type,
                    }
                )
        return joins
    except Exception:  # noqa: BLE001
        logger.debug("Failed to extract join conditions", exc_info=True)
        return []


async def save_pattern(
    query_text: str,
    sql: str,
    tables_used: list[str],
    matched_groups: list[str],
    pool: asyncpg.Pool,
) -> None:
    try:
        join_conditions = extract_join_conditions(sql)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO nl2sql_learned_patterns
                    (query_text, sql_used, tables_used, join_conditions, matched_groups)
                VALUES ($1, $2, $3, $4::jsonb, $5)
                ON CONFLICT DO NOTHING
                """,
                query_text,
                sql,
                tables_used,
                json.dumps(join_conditions),
                matched_groups,
            )
        logger.info("Pattern saved: %s", query_text[:60])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to save learned pattern: %s", exc)


async def get_relevant_patterns(
    query: str,
    tables_in_scope: list[str],
    pool: asyncpg.Pool,
    limit: int = 3,
    min_use_count: int = 2,
) -> list[dict]:
    del query
    if not tables_in_scope:
        return []

    try:
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
                  AND use_count >= $2
                  AND tables_used && $1::text[]
                ORDER BY use_count DESC, last_used_at DESC
                LIMIT $3
                """,
                tables_in_scope,
                min_use_count,
                limit,
            )
        return [_pattern_row_to_dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load relevant learned patterns: %s", exc)
        return []


async def increment_pattern_use(
    pattern_id: int,
    pool: asyncpg.Pool,
) -> None:
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE nl2sql_learned_patterns
                SET use_count = use_count + 1,
                    last_used_at = NOW()
                WHERE id = $1
                """,
                pattern_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to increment learned pattern %s: %s", pattern_id, exc)


def format_patterns_for_prompt(
    patterns: list[dict],
) -> str:
    if not patterns:
        return ""

    rendered: list[str] = []
    for index, pattern in enumerate(patterns, start=1):
        join_conditions = _coerce_json(pattern.get("join_conditions"), default=[])
        join_lines = []
        for join in join_conditions:
            join_lines.append(
                "Join: "
                f"{join.get('left_table')}.{join.get('left_column')} = "
                f"{join.get('right_table')}.{join.get('right_column')}"
            )
        if not join_lines:
            join_lines.append("Join: (none)")

        tables_used = list(pattern.get("tables_used") or [])
        block_lines = [
            f"Learned pattern #{index} (used {pattern.get('use_count', 0)} times):",
            f"Example query: {str(pattern.get('query_text', ''))[:80]}",
            f"Tables: {', '.join(tables_used)}",
            *join_lines,
            f"SQL: {str(pattern.get('sql_used', ''))[:200]}",
        ]
        rendered.append("\n".join(block_lines))

    return "\n\n".join(rendered)


def _extract_alias_map(sql: str) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    for match in _TABLE_REF_RE.finditer(sql):
        table = _normalize_identifier(match.group(1))
        alias = _normalize_identifier(match.group(2) or "")
        if not table:
            continue
        alias_map[table] = table
        if alias and alias.upper() not in _SQL_BOUNDARY_WORDS:
            alias_map[alias] = table
    return alias_map


def _resolve_joined_table(
    join_match: re.Match[str],
    alias_map: dict[str, str],
) -> str:
    table = _normalize_identifier(join_match.group(2))
    alias = _normalize_identifier(join_match.group(3) or "")
    if alias:
        return alias_map.get(alias, table)
    return _strip_schema_prefix(table)


def _parse_column_ref(
    raw_ref: str,
    alias_map: dict[str, str],
) -> tuple[str, str] | None:
    parts = [_normalize_identifier(part) for part in raw_ref.split(".")]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return None

    table_token = parts[-2]
    column = parts[-1]
    table = alias_map.get(table_token, _strip_schema_prefix(table_token))
    if not table or not column:
        return None
    return table, column


def _normalize_identifier(identifier: str) -> str:
    return identifier.strip().strip("`\"[]").lower()


def _strip_schema_prefix(identifier: str) -> str:
    return _normalize_identifier(identifier).split(".")[-1]


def _pattern_row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
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


def _coerce_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value
