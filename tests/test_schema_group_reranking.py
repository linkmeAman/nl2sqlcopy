from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nl2sql_service import retrieve


class _NoopTransaction:
    async def __aenter__(self) -> "_NoopTransaction":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _GroupConn:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def transaction(self) -> _NoopTransaction:
        return _NoopTransaction()

    async def execute(self, sql: str) -> str:
        del sql
        return "OK"

    async def fetch(self, sql: str, *args):
        del sql, args
        return self._rows


class _Acquire:
    def __init__(self, conn: _GroupConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _GroupConn:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _Pool:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._conn = _GroupConn(rows)

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


@pytest.mark.asyncio
async def test_retrieve_groups_prioritizes_direct_table_matches_for_short_queries(
    mock_embed,
    mock_pattern_store_empty,
    mock_instruction_store_empty,
) -> None:
    del mock_embed, mock_pattern_store_empty, mock_instruction_store_empty
    rows = [
        {
            "content": "member profile context",
            "similarity": 0.62,
            "source": "member_profile",
            "chunk_index": 0,
            "token_count": 10,
            "embedding_model": "test",
            "metadata": {
                "root_table": "member",
                "tables": ["member", "contact"],
                "related_tables": ["invoice", "payment"],
            },
        },
        {
            "content": "billing context",
            "similarity": 0.58,
            "source": "legacy_invoice_billing",
            "chunk_index": 0,
            "token_count": 10,
            "embedding_model": "test",
            "metadata": {
                "root_table": "invoice",
                "tables": ["invoice", "payment"],
                "related_tables": ["member", "branch"],
            },
        },
        {
            "content": "employee context",
            "similarity": 0.61,
            "source": "employee_access_branch",
            "chunk_index": 0,
            "token_count": 10,
            "embedding_model": "test",
            "metadata": {
                "root_table": "employee",
                "tables": ["employee", "employee_bid"],
                "related_tables": ["contact", "payment"],
            },
        },
    ]

    result = await retrieve.retrieve_groups(
        query="latest payment",
        top_k=3,
        pool=_Pool(rows),
    )

    assert result.matched_groups[0] == "legacy_invoice_billing"
    assert result.tables_in_scope[:2] == ["invoice", "payment"]
    assert result.context.index("## Schema group: legacy_invoice_billing") < result.context.index(
        "## Schema group: member_profile"
    )


@pytest.mark.asyncio
async def test_retrieve_groups_generic_reranking_works_for_other_entities(
    mock_embed,
    mock_pattern_store_empty,
    mock_instruction_store_empty,
) -> None:
    del mock_embed, mock_pattern_store_empty, mock_instruction_store_empty
    rows = [
        {
            "content": "billing context",
            "similarity": 0.67,
            "source": "legacy_invoice_billing",
            "chunk_index": 0,
            "token_count": 10,
            "embedding_model": "test",
            "metadata": {
                "root_table": "invoice",
                "tables": ["invoice", "payment"],
                "related_tables": ["member"],
            },
        },
        {
            "content": "member context",
            "similarity": 0.61,
            "source": "member_profile",
            "chunk_index": 0,
            "token_count": 10,
            "embedding_model": "test",
            "metadata": {
                "root_table": "member",
                "tables": ["member", "contact"],
                "related_tables": ["invoice", "batch"],
            },
        },
    ]

    result = await retrieve.retrieve_groups(
        query="recent member",
        top_k=2,
        pool=_Pool(rows),
    )

    assert result.matched_groups[0] == "member_profile"


def test_schema_group_reranking_skips_broad_queries() -> None:
    rows = [
        {
            "similarity": 0.70,
            "source": "billing",
            "metadata": {"root_table": "invoice", "tables": ["invoice", "payment"]},
        },
        {
            "similarity": 0.60,
            "source": "membership",
            "metadata": {"root_table": "member", "tables": ["member", "contact"]},
        },
    ]

    ranked = retrieve._rerank_schema_group_rows(
        query="show payment invoice member branch employee contact service status",
        rows=rows,
    )

    assert ranked == rows
