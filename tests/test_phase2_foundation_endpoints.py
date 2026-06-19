from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_telemetry_recent_endpoint_returns_results(
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service.db import db
    from nl2sql_service import main

    mock_list_recent = AsyncMock(
        return_value=[
            {
                "request_id": "abc123",
                "endpoint": "/ask",
                "status": "ok",
                "latency_ms": 220,
            }
        ]
    )
    monkeypatch.setattr(db, "list_recent_request_events", mock_list_recent)

    response = await client.get("/telemetry/recent?limit=5&endpoint=/ask")

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["request_id"] == "abc123"
    assert body["results"][0]["endpoint"] == "/ask"
    assert mock_list_recent.await_count == 1


@pytest.mark.asyncio
async def test_telemetry_summary_endpoint_returns_kpis(
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service.db import db

    mock_summary = AsyncMock(
        return_value={
            "total_requests": 100,
            "ok_count": 70,
            "clarification_count": 20,
            "rejected_count": 10,
            "ok_rate": 0.7,
            "clarification_rate": 0.2,
            "rejected_rate": 0.1,
            "avg_latency_ms": 320,
            "p50_latency_ms": 260,
            "p95_latency_ms": 810,
            "error_sources": [{"error_source": "execution", "count": 4}],
        }
    )
    monkeypatch.setattr(db, "get_telemetry_summary", mock_summary)

    response = await client.get("/telemetry/summary?endpoint=/ask&since_minutes=60")

    assert response.status_code == 200
    body = response.json()
    assert body["total_requests"] == 100
    assert body["ok_rate"] == 0.7
    assert body["p95_latency_ms"] == 810
    assert body["endpoint"] == "/ask"
    assert body["since_minutes"] == 60
    assert mock_summary.await_count == 1


@pytest.mark.asyncio
async def test_benchmark_add_case_endpoint_persists_case(
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service.db import db

    mock_insert_case = AsyncMock(return_value=42)
    monkeypatch.setattr(db, "insert_benchmark_case", mock_insert_case)

    response = await client.post(
        "/benchmark/cases",
        json={
            "query": "show unpaid invoices by counselor",
            "gold_sql": "SELECT id FROM invoice WHERE status='unpaid'",
            "expected_status": "ok",
            "slices": ["joins", "aggregation"],
            "error_label": None,
            "source": "manual",
            "metadata": {"owner": "phase2"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 42
    assert body["query"] == "show unpaid invoices by counselor"
    assert body["expected_status"] == "ok"
    assert mock_insert_case.await_count == 1


@pytest.mark.asyncio
async def test_benchmark_list_cases_endpoint_returns_results(
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service.db import db

    mock_list_cases = AsyncMock(
        return_value=[
            {
                "id": 1,
                "query_text": "newest payment",
                "expected_status": "ok",
                "slices": ["single_table"],
            }
        ]
    )
    monkeypatch.setattr(db, "list_benchmark_cases", mock_list_cases)

    response = await client.get("/benchmark/cases?limit=10&active_only=true")

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["id"] == 1
    assert body["results"][0]["query_text"] == "newest payment"
    assert mock_list_cases.await_count == 1
