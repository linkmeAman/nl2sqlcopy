from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from nl2sql_service.core.config import Settings

log = logging.getLogger(__name__)


async def load_column_catalog(
    settings: Settings,
    tables: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Load live MySQL column metadata from information_schema."""
    schema_name = (settings.db_name or settings.db_central or "").strip()
    if not schema_name:
        log.warning("DB_NAME/DB_CENTRAL not set; cannot load live MySQL columns")
        return []

    clean_tables = [table.strip().lower() for table in (tables or []) if table and table.strip()]
    try:
        import aiomysql
    except ImportError:
        log.warning("aiomysql is not installed; live MySQL column enrichment disabled")
        return []

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

        query = (
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, ORDINAL_POSITION "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s"
        )
        params: list[Any] = [schema_name]
        if clean_tables:
            query += " AND TABLE_NAME IN (" + ",".join(["%s"] * len(clean_tables)) + ")"
            params.extend(clean_tables)
        query += " ORDER BY TABLE_NAME, ORDINAL_POSITION"

        async with connection.cursor() as cursor:
            await cursor.execute(query, params)
            rows = await cursor.fetchall()

        return [
            {
                "table_name": str(table_name).lower(),
                "column_name": str(column_name).lower(),
                "data_type": str(data_type).lower(),
                "ordinal_position": int(ordinal_position),
            }
            for table_name, column_name, data_type, ordinal_position in rows
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning("Unable to load live MySQL column catalog: %s", exc)
        return []
    finally:
        if connection is not None:
            connection.close()


async def load_columns_for_tables(
    tables: list[str],
    settings: Settings,
) -> dict[str, list[str]]:
    """
    Load live MySQL column names for the provided tables only.

    Returns:
        Dict mapping ``table_name.lower()`` -> ``[column_name.lower(), ...]``.
        Returns ``{}`` when MySQL is unreachable or configuration is incomplete.
    """
    if not tables:
        return {}

    schema_name = (settings.db_name or settings.db_central or "").strip()
    if not schema_name:
        log.warning("DB_NAME/DB_CENTRAL not set; cannot load live MySQL columns")
        return {}

    clean_tables = [table.strip().lower() for table in tables if table and table.strip()]
    if not clean_tables:
        return {}

    catalog = await load_column_catalog(settings, tables=clean_tables)
    columns_by_table: defaultdict[str, list[str]] = defaultdict(list)
    for record in catalog:
        columns_by_table[str(record["table_name"])].append(str(record["column_name"]))
    return dict(columns_by_table)
