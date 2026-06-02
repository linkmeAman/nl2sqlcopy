from __future__ import annotations

import importlib.util
import re
from typing import Any

import sqlparse

from nl2sql_service.config import Settings
from nl2sql_service.models import SqlWarning, WarningCode

_LIMIT_OFFSET_TRAILING_RE = re.compile(
    r"\bLIMIT\s+(\d+)(\s+OFFSET\s+\d+)?\s*$",
    re.IGNORECASE,
)
_LIMIT_COMMA_TRAILING_RE = re.compile(
    r"\bLIMIT\s+(\d+)\s*,\s*(\d+)\s*$",
    re.IGNORECASE,
)


def apply_row_cap(sql: str, cap: int = 50) -> str:
    """Apply a top-level row cap to SQL execution while keeping SQL guardrails untouched."""
    stripped = sql.strip()
    if not stripped:
        return sql

    # Keep parser call so malformed SQL does not accidentally get rewritten.
    if not sqlparse.parse(stripped):
        return sql

    has_semicolon = stripped.endswith(";")
    base_sql = stripped[:-1].rstrip() if has_semicolon else stripped

    comma_match = _LIMIT_COMMA_TRAILING_RE.search(base_sql)
    if comma_match:
        offset = int(comma_match.group(1))
        count = int(comma_match.group(2))
        if count <= cap:
            return stripped
        start, end = comma_match.span()
        capped = f"{base_sql[:start]}LIMIT {offset}, {cap}{base_sql[end:]}"
        return f"{capped};" if has_semicolon else capped

    offset_match = _LIMIT_OFFSET_TRAILING_RE.search(base_sql)
    if offset_match:
        limit = int(offset_match.group(1))
        offset_suffix = offset_match.group(2) or ""
        if limit <= cap:
            return stripped
        start, end = offset_match.span()
        capped = f"{base_sql[:start]}LIMIT {cap}{offset_suffix}{base_sql[end:]}"
        return f"{capped};" if has_semicolon else capped

    capped = f"{base_sql} LIMIT {cap}"
    return f"{capped};" if has_semicolon else capped


async def execute_sql(
    sql: str,
    settings: Settings,
) -> tuple[list[str], list[tuple[Any, ...]], list[SqlWarning]]:
    """Execute SQL against the app MySQL database and return columns + rows."""
    schema_name = (settings.db_name or settings.db_central or "").strip()
    if not schema_name:
        return [], [], [
            SqlWarning(
                code=WarningCode.MYSQL_QUERY_ERROR,
                message="DB_NAME/DB_CENTRAL not set; cannot execute SQL",
            )
        ]

    try:
        import aiomysql
    except ImportError:
        return [], [], [
            SqlWarning(
                code=WarningCode.MYSQL_QUERY_ERROR,
                message="aiomysql is not installed; SQL execution unavailable",
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
        async with connection.cursor() as cursor:
            await cursor.execute(sql)
            rows = list(await cursor.fetchall())
            columns = [col[0] for col in (cursor.description or [])]
        return columns, rows, []
    except Exception as exc:  # noqa: BLE001
        return [], [], [
            SqlWarning(
                code=WarningCode.MYSQL_QUERY_ERROR,
                message=f"MySQL query failed: {exc}",
            )
        ]
    finally:
        if connection is not None:
            connection.close()


async def mysql_target_readiness(
    settings: Settings,
    *,
    timeout_seconds: float = 3.0,
) -> dict[str, object]:
    schema_name = (settings.db_name or settings.db_central or "").strip()
    issues: list[dict[str, str]] = []
    host = (settings.db_host or "").strip()
    user = (settings.db_user or "").strip()

    if not schema_name:
        issues.append(
            {
                "code": "MYSQL_SCHEMA_REQUIRED",
                "message": "DB_NAME or DB_CENTRAL must be set for MySQL execution.",
            }
        )
    if not host:
        issues.append(
            {
                "code": "MYSQL_HOST_REQUIRED",
                "message": "DB_HOST must be set for MySQL execution.",
            }
        )
    if not user:
        issues.append(
            {
                "code": "MYSQL_USER_REQUIRED",
                "message": "DB_USER must be set for MySQL execution.",
            }
        )
    if settings.db_port <= 0:
        issues.append(
            {
                "code": "MYSQL_PORT_INVALID",
                "message": "DB_PORT must be a positive integer.",
            }
        )

    if importlib.util.find_spec("aiomysql") is None:
        issues.append(
            {
                "code": "MYSQL_DRIVER_MISSING",
                "message": "aiomysql is not installed; SQL execution is unavailable.",
            }
        )

    if issues:
        return {
            "status": "error",
            "host": host or None,
            "port": settings.db_port,
            "schema": schema_name or None,
            "issues": issues,
        }

    try:
        import aiomysql
    except ImportError:
        return {
            "status": "error",
            "host": host,
            "port": settings.db_port,
            "schema": schema_name,
            "issues": [
                {
                    "code": "MYSQL_DRIVER_MISSING",
                    "message": "aiomysql is not installed; SQL execution is unavailable.",
                }
            ],
        }

    connection = None
    try:
        connection = await aiomysql.connect(
            host=host,
            port=settings.db_port,
            user=user,
            password=settings.db_password,
            db=schema_name,
            autocommit=True,
            connect_timeout=timeout_seconds,
        )
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1")
            await cursor.fetchone()
        return {
            "status": "ok",
            "host": host,
            "port": settings.db_port,
            "schema": schema_name,
            "issues": [],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "host": host,
            "port": settings.db_port,
            "schema": schema_name,
            "issues": [
                {
                    "code": "MYSQL_CONNECT_FAILED",
                    "message": f"MySQL readiness check failed: {exc}",
                }
            ],
        }
    finally:
        if connection is not None:
            connection.close()
