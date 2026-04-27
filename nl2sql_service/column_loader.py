from __future__ import annotations

import logging
from collections import defaultdict

from nl2sql_service.config import Settings

log = logging.getLogger(__name__)


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

    try:
        import aiomysql
    except ImportError:
        log.warning("aiomysql is not installed; live MySQL column enrichment disabled")
        return {}

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
            "SELECT TABLE_NAME, COLUMN_NAME "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN ("
            + ",".join(["%s"] * len(clean_tables))
            + ") ORDER BY TABLE_NAME, ORDINAL_POSITION"
        )

        columns_by_table: defaultdict[str, list[str]] = defaultdict(list)
        async with connection.cursor() as cursor:
            await cursor.execute(query, [schema_name, *clean_tables])
            rows = await cursor.fetchall()

        for table_name, column_name in rows:
            table_key = str(table_name).lower()
            col_key = str(column_name).lower()
            columns_by_table[table_key].append(col_key)

        return dict(columns_by_table)
    except Exception as exc:  # noqa: BLE001
        log.warning("Unable to load live MySQL columns: %s", exc)
        return {}
    finally:
        if connection is not None:
            connection.close()
