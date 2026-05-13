"""
tests/test_endpoints_core.py
============================
Integration tests for core NL2SQL endpoints.

Covers:
  GET  /health
  POST /ingest  (text + schema variants)
  POST /query
  POST /ingest/groups
  POST /ingest/knowledge
  POST /query/groups
  GET  /ingest/groups/status
"""

from __future__ import annotations

import json
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(resp: httpx.Response) -> dict:
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    data = _ok(resp)
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_db_unavailable(app) -> None:
    """When the pool is missing the app must still return a degraded-but-200 health response."""
    original = getattr(app.state, "pool", None)
    app.state.pool = None
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            resp = await c.get("/health")
        # /health should report degraded rather than 500
        assert resp.status_code in (200, 503), resp.text
    finally:
        app.state.pool = original


# ---------------------------------------------------------------------------
# /ingest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_text(
    client: httpx.AsyncClient,
    mock_embed: AsyncMock,
) -> None:
    """POST /ingest with type=text should embed and persist chunks."""
    from nl2sql_service import db

    with patch.object(db, "insert_chunks", new_callable=AsyncMock) as mock_insert:
        mock_insert.return_value = 1
        resp = await client.post(
            "/ingest",
            json={
                "type": "text",
                "source": "test_doc",
                "text": "This is a sample knowledge document for testing.",
            },
        )
    data = _ok(resp)
    assert data["source"] == "test_doc"
    assert data["inserted"] >= 0


@pytest.mark.asyncio
async def test_ingest_schema(
    client: httpx.AsyncClient,
    mock_embed: AsyncMock,
) -> None:
    """POST /ingest with type=schema should embed the provided table definitions."""
    from nl2sql_service import db

    tables = [
        {
            "database": "crm",
            "object_name": "invoice",
            "full_object_name": "crm.invoice",
            "text": "invoice(id, member_id, total, status)",
            "chunk_index": 1,
            "total_chunks": 1,
            "source_kind": "schema_export",
        }
    ]
    with patch.object(db, "insert_chunks", new_callable=AsyncMock) as mock_insert:
        mock_insert.return_value = 1
        resp = await client.post(
            "/ingest",
            json={"type": "schema", "source": "test_schema", "tables": tables},
        )
    data = _ok(resp)
    assert data["source"] == "test_schema"
    assert isinstance(data["inserted"], int)


# ---------------------------------------------------------------------------
# /query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query(client: httpx.AsyncClient, mock_embed: AsyncMock) -> None:
    """POST /query should return a results list (may be empty with mocked embeddings)."""
    from nl2sql_service import retrieve

    with patch.object(
        retrieve,
        "retrieve",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = await client.post("/query", json={"query": "find unpaid invoices"})
    data = _ok(resp)
    assert "results" in data
    assert isinstance(data["results"], list)


# ---------------------------------------------------------------------------
# /ingest/groups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_groups(
    client: httpx.AsyncClient,
    mock_embed: AsyncMock,
    tmp_rag_schema,
    mock_load_columns_for_ingest: AsyncMock,
) -> None:
    """POST /ingest/groups with no body should ingest all entities without error."""
    from nl2sql_service import ingest

    stub_result = {
        "inserted_count": 2,
        "updated_count": 0,
        "enrichment_summary": {
            "groups_with_columns": 1,
            "groups_without_columns": 0,
            "groups_with_aliases": 0,
            "groups_with_examples": 0,
        },
    }
    with patch.object(ingest, "ingest_schema_groups", new_callable=AsyncMock, return_value=stub_result):
        resp = await client.post("/ingest/groups", json={})
    data = _ok(resp)
    assert data["inserted"] == 2


@pytest.mark.asyncio
async def test_ingest_groups_includes_partial_failures(
    client: httpx.AsyncClient,
    tmp_rag_schema,
    mock_load_columns_for_ingest: AsyncMock,
) -> None:
    """POST /ingest/groups should include failed_groups for overflowed entities."""
    from nl2sql_service import ingest

    del tmp_rag_schema
    del mock_load_columns_for_ingest

    stub_result = {
        "inserted_count": 1,
        "updated_count": 0,
        "failed_groups": [
            {
                "group_name": "entity__inquiry_lifecycle",
                "reason": "Group 'entity__inquiry_lifecycle' estimated 789 tokens after enrichment. Exceeds 400 limit.",
            }
        ],
        "enrichment_summary": {
            "groups_with_columns": 1,
            "groups_without_columns": 0,
            "groups_with_aliases": 0,
            "groups_with_examples": 1,
        },
    }
    with patch.object(ingest, "ingest_schema_groups", new_callable=AsyncMock, return_value=stub_result):
        resp = await client.post("/ingest/groups", json={"group_names": ["inquiry_lifecycle"]})

    data = _ok(resp)
    assert data["inserted"] == 1
    assert data["failure_count"] == 1
    assert data["failed_groups"][0]["group_name"] == "entity__inquiry_lifecycle"


# ---------------------------------------------------------------------------
# /ingest/knowledge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_knowledge_defaults(
    client: httpx.AsyncClient,
    mock_embed: AsyncMock,
    tmp_rag_schema,
    mock_load_columns_for_ingest: AsyncMock,
) -> None:
    """POST /ingest/knowledge with defaults should embed knowledge chunks."""
    from nl2sql_service import ingest

    stub_result = {"inserted_count": 5, "updated_count": 0}
    with patch.object(ingest, "ingest_enriched_knowledge", new_callable=AsyncMock, return_value=stub_result):
        resp = await client.post("/ingest/knowledge", json={})
    data = _ok(resp)
    assert data["source"] == "knowledge"
    assert data["inserted"] == 5


# ---------------------------------------------------------------------------
# /query/groups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_groups(
    client: httpx.AsyncClient,
    mock_retrieve_groups: AsyncMock,
) -> None:
    """POST /query/groups should return matched_groups and tables_in_scope."""
    resp = await client.post("/query/groups", json={"query": "show overdue payments"})
    data = _ok(resp)
    assert "matched_groups" in data
    assert "tables_in_scope" in data
    assert isinstance(data["matched_groups"], list)
    assert isinstance(data["tables_in_scope"], list)


# ---------------------------------------------------------------------------
# GET /ingest/groups/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_groups_status_all_never_embedded(
    client: httpx.AsyncClient,
    tmp_rag_schema,
) -> None:
    """When no embeddings exist yet every group should be never_embedded."""
    from nl2sql_service import db

    with patch.object(
        db,
        "get_group_embedding_status",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = await client.get("/ingest/groups/status")
    data = _ok(resp)
    assert "groups" in data
    assert data["never_embedded_count"] == len(data["groups"])
    assert data["current_count"] == 0
    assert data["stale_count"] == 0


@pytest.mark.asyncio
async def test_ingest_groups_status_current(
    client: httpx.AsyncClient,
    tmp_rag_schema,
) -> None:
    """When stored_version matches the file hash the group should be current."""
    from nl2sql_service import db, schema_loader

    # Compute the real file hash so we can plant a matching stored_version.
    real_hash = schema_loader.get_schema_version("billing")
    mock_rows = [
        {
            "source": "billing",
            "stored_version": real_hash,
            "last_embedded_at": None,
        }
    ]
    with patch.object(
        db,
        "get_group_embedding_status",
        new_callable=AsyncMock,
        return_value=mock_rows,
    ):
        resp = await client.get("/ingest/groups/status")
    data = _ok(resp)
    groups = {g["group_name"]: g for g in data["groups"]}
    assert groups["billing"]["is_current"] is True
    assert data["current_count"] == 1
    assert data["stale_count"] == 0


@pytest.mark.asyncio
async def test_ingest_groups_status_stale(
    client: httpx.AsyncClient,
    tmp_rag_schema,
) -> None:
    """When stored_version is outdated the group should be stale."""
    from nl2sql_service import db

    mock_rows = [
        {
            "source": "billing",
            "stored_version": "deadbeef",  # definitely wrong hash
            "last_embedded_at": None,
        }
    ]
    with patch.object(
        db,
        "get_group_embedding_status",
        new_callable=AsyncMock,
        return_value=mock_rows,
    ):
        resp = await client.get("/ingest/groups/status")
    data = _ok(resp)
    groups = {g["group_name"]: g for g in data["groups"]}
    assert groups["billing"]["is_current"] is False
    assert data["stale_count"] == 1
    assert data["current_count"] == 0
