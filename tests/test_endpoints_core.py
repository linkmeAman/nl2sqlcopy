"""
tests/test_endpoints_core.py
============================
Integration tests for core NL2SQL endpoints.

Covers:
  GET  /health
  GET  /metrics/teach
  POST /ingest  (text + schema variants)
  POST /query
  POST /ingest/groups
  POST /ingest/knowledge
  POST /query/groups
  GET  /ingest/groups/status
  GET  /teach/pending
  POST /teach/pending/cleanup
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
    from nl2sql_service import mysql_executor, schema_loader

    with patch.object(
        mysql_executor,
        "mysql_target_readiness",
        AsyncMock(return_value={"status": "ok", "issues": []}),
    ), patch.object(
        schema_loader,
        "loader_readiness",
        MagicMock(return_value={"status": "ok", "issues": []}),
    ):
        resp = await client.get("/health")
    data = _ok(resp)
    assert data["status"] == "ok"
    assert data["provider_config"]["status"] == "ok"
    assert data["mysql_target"]["status"] == "ok"
    assert data["schema_assets"]["status"] == "ok"
    assert "teach_confirmations" in data


@pytest.mark.asyncio
async def test_health_db_unavailable(app) -> None:
    """When the pool is missing the app must still return a degraded-but-200 health response."""
    from nl2sql_service import mysql_executor, schema_loader

    original = getattr(app.state, "pool", None)
    app.state.pool = None
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            with patch.object(
                mysql_executor,
                "mysql_target_readiness",
                AsyncMock(return_value={"status": "ok", "issues": []}),
            ), patch.object(
                schema_loader,
                "loader_readiness",
                MagicMock(return_value={"status": "ok", "issues": []}),
            ):
                resp = await c.get("/health")
        # /health should report degraded rather than 500
        assert resp.status_code in (200, 503), resp.text
    finally:
        app.state.pool = original


@pytest.mark.asyncio
async def test_teach_metrics_endpoint(client: httpx.AsyncClient) -> None:
    from nl2sql_service import db

    mock_stats = AsyncMock(
        return_value={
            "pending_active_count": 2,
            "pending_expired_count": 1,
            "oldest_pending_created_at": "2026-06-01T10:00:00Z",
            "next_pending_expiry_at": "2026-06-01T10:30:00Z",
        }
    )
    with patch.object(db, "get_pending_teach_confirmation_stats", mock_stats):
        resp = await client.get("/metrics/teach")
    data = _ok(resp)
    assert data["pending_active_count"] == 2
    assert data["pending_expired_count"] == 1
    assert data["status"] == "warning"
    assert len(data["alerts"]) == 1
    assert data["alerts"][0]["code"] == "TEACH_PENDING_EXPIRED"
    assert data["thresholds"]["pending_expired_warn_threshold"] >= 1


@pytest.mark.asyncio
async def test_health_config_endpoint(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health/config")
    data = _ok(resp)
    assert data["status"] == "ok"
    assert any(target["target"] == "LLM_PROVIDER" for target in data["targets"])


@pytest.mark.asyncio
async def test_get_model_routing_endpoint(client: httpx.AsyncClient) -> None:
    resp = await client.get("/config/model-routing")
    data = _ok(resp)
    assert "sql" in data
    assert "reasoning" in data
    assert "provider_readiness" in data


@pytest.mark.asyncio
async def test_get_ask_model_endpoint(client: httpx.AsyncClient) -> None:
    resp = await client.get("/config/ask-model")
    data = _ok(resp)
    assert "provider" in data
    assert "model" in data
    assert "api_key_configured" in data


@pytest.mark.asyncio
async def test_patch_model_routing_endpoint_updates_runtime_config(client: httpx.AsyncClient) -> None:
    from nl2sql_service.config import settings

    previous = {
        "sql_model_provider": settings.sql_model_provider,
        "sql_model": settings.sql_model,
        "sql_model_base_url": settings.sql_model_base_url,
    }
    try:
        resp = await client.patch(
            "/config/model-routing",
            json={
                "sql_model_provider": "ollama",
                "sql_model": "qwen2.5-coder:7b",
                "sql_model_base_url": "http://localhost:11434",
            },
        )
        data = _ok(resp)
        assert data["sql"]["provider"] == "ollama"
        assert data["sql"]["model"] == "qwen2.5-coder:7b"
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)


@pytest.mark.asyncio
async def test_patch_ask_model_endpoint_updates_runtime_config(client: httpx.AsyncClient) -> None:
    from nl2sql_service.config import settings

    previous = {
        "answer_model_provider": settings.answer_model_provider,
        "answer_model": settings.answer_model,
        "answer_model_base_url": settings.answer_model_base_url,
    }
    try:
        resp = await client.patch(
            "/config/ask-model",
            json={
                "provider": "ollama",
                "model": "qwen3:4b",
                "base_url": "http://localhost:11434",
            },
        )
        data = _ok(resp)
        assert data["provider"] == "ollama"
        assert data["model"] == "qwen3:4b"
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)


@pytest.mark.asyncio
async def test_patch_model_routing_rejects_invalid_config(client: httpx.AsyncClient) -> None:
    from nl2sql_service.config import settings

    previous = {
        "llm_base_url": settings.llm_base_url,
        "sql_model_base_url": settings.sql_model_base_url,
        "reasoning_model_base_url": settings.reasoning_model_base_url,
    }
    try:
        settings.llm_base_url = None
        settings.sql_model_base_url = None
        settings.reasoning_model_base_url = None
        resp = await client.patch(
            "/config/model-routing",
            json={
                "sql_model_provider": "ollama",
                "sql_model": "qwen2.5-coder:7b",
            },
        )
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["message"] == "Invalid model routing configuration"


@pytest.mark.asyncio
async def test_patch_ask_model_rejects_invalid_config(client: httpx.AsyncClient) -> None:
    from nl2sql_service.config import settings

    previous = {
        "llm_base_url": settings.llm_base_url,
        "answer_model_base_url": settings.answer_model_base_url,
        "reasoning_model_base_url": settings.reasoning_model_base_url,
    }
    try:
        settings.llm_base_url = None
        settings.answer_model_base_url = None
        settings.reasoning_model_base_url = None
        resp = await client.patch(
            "/config/ask-model",
            json={
                "provider": "ollama",
                "model": "qwen3:4b",
            },
        )
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["message"] == "Invalid ask model configuration"


@pytest.mark.asyncio
async def test_health_runtime_endpoint(client: httpx.AsyncClient) -> None:
    from nl2sql_service import mysql_executor, schema_loader

    with patch.object(
        mysql_executor,
        "mysql_target_readiness",
        AsyncMock(
            return_value={
                "status": "ok",
                "host": "localhost",
                "port": 3306,
                "schema": "demo",
                "issues": [],
            }
        ),
    ), patch.object(
        schema_loader,
        "loader_readiness",
        MagicMock(
            return_value={
                "status": "ok",
                "rag_schema_dir": "/tmp/rag_schema",
                "docs_dir": "/tmp/docs",
                "entity_count": 5,
                "relation_count": 4,
                "missing_docs": [],
                "issues": [],
            }
        ),
    ):
        resp = await client.get("/health/runtime")
    data = _ok(resp)
    assert data["status"] == "ok"
    assert data["mysql_target"]["schema"] == "demo"
    assert data["schema_assets"]["entity_count"] == 5


@pytest.mark.asyncio
async def test_health_warns_on_teach_confirmation_alerts(client: httpx.AsyncClient) -> None:
    from nl2sql_service import db, mysql_executor, schema_loader

    mock_stats = AsyncMock(
        return_value={
            "pending_active_count": 30,
            "pending_expired_count": 2,
            "oldest_pending_created_at": "2026-06-01T10:00:00Z",
            "next_pending_expiry_at": "2026-06-01T10:30:00Z",
        }
    )
    with patch.object(db, "get_pending_teach_confirmation_stats", mock_stats), patch.object(
        mysql_executor,
        "mysql_target_readiness",
        AsyncMock(return_value={"status": "ok", "issues": []}),
    ), patch.object(
        schema_loader,
        "loader_readiness",
        MagicMock(return_value={"status": "ok", "issues": []}),
    ):
        resp = await client.get("/health")
    data = _ok(resp)
    assert data["status"] == "warning"
    assert data["teach_confirmations"]["status"] == "warning"
    assert len(data["teach_confirmations"]["alerts"]) == 2


@pytest.mark.asyncio
async def test_health_errors_on_runtime_dependency_failure(client: httpx.AsyncClient) -> None:
    from nl2sql_service import mysql_executor, schema_loader

    with patch.object(
        mysql_executor,
        "mysql_target_readiness",
        AsyncMock(
            return_value={
                "status": "error",
                "issues": [{"code": "MYSQL_DRIVER_MISSING", "message": "aiomysql missing"}],
            }
        ),
    ), patch.object(
        schema_loader,
        "loader_readiness",
        MagicMock(return_value={"status": "ok", "issues": []}),
    ):
        resp = await client.get("/health")
    data = _ok(resp)
    assert data["status"] == "error"
    assert data["mysql_target"]["status"] == "error"


@pytest.mark.asyncio
async def test_teach_pending_list_endpoint(client: httpx.AsyncClient) -> None:
    from nl2sql_service import db

    mock_list = AsyncMock(
        return_value=[
            {
                "token": "abc123",
                "instruction_type": "table_relationship",
                "content": "employee.employee_id = contact.id",
                "tables_affected": ["employee", "contact"],
                "source_query": None,
                "conflicting_id": 12,
                "created_at": "2026-06-01T10:00:00Z",
                "expires_at": "2026-06-01T10:30:00Z",
                "is_expired": False,
            }
        ]
    )
    mock_stats = AsyncMock(
        return_value={
            "pending_active_count": 1,
            "pending_expired_count": 0,
            "oldest_pending_created_at": "2026-06-01T10:00:00Z",
            "next_pending_expiry_at": "2026-06-01T10:30:00Z",
        }
    )
    with patch.object(db, "list_pending_teach_confirmations", mock_list), patch.object(
        db, "get_pending_teach_confirmation_stats", mock_stats
    ):
        resp = await client.get("/teach/pending?limit=20&include_expired=false")
    data = _ok(resp)
    assert data["results"][0]["token"] == "abc123"
    assert data["stats"]["pending_active_count"] == 1


@pytest.mark.asyncio
async def test_teach_pending_cleanup_endpoint(client: httpx.AsyncClient) -> None:
    from nl2sql_service import db

    mock_cleanup = AsyncMock(return_value=3)
    mock_stats = AsyncMock(
        return_value={
            "pending_active_count": 2,
            "pending_expired_count": 0,
            "oldest_pending_created_at": "2026-06-01T10:00:00Z",
            "next_pending_expiry_at": "2026-06-01T10:30:00Z",
        }
    )
    with patch.object(db, "cleanup_pending_teach_confirmations", mock_cleanup), patch.object(
        db, "get_pending_teach_confirmation_stats", mock_stats
    ):
        resp = await client.post("/teach/pending/cleanup")
    data = _ok(resp)
    assert data["deleted"] == 3
    assert data["stats"]["pending_expired_count"] == 0


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
