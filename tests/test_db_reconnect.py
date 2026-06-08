from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from nl2sql_service.models import GenerateSqlSuccess


@pytest.mark.asyncio
async def test_auto_reconnect_recovers_without_restart(
    app,
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import main
    from nl2sql_service.config import settings

    app.state.pool = None
    app.state.pool_last_reconnect_attempt = 0.0
    settings.db_reconnect_min_interval = 0.0

    restored_pool = object()
    mock_create_pool = AsyncMock(return_value=restored_pool)
    mock_bootstrap = AsyncMock(return_value=None)
    mock_ensure_hnsw = AsyncMock(return_value=None)
    mock_generate_sql = AsyncMock(
        return_value=GenerateSqlSuccess(
            sql="SELECT id FROM payment LIMIT 1",
            warnings=[],
            tables_used=["payment"],
            matched_groups=["sales_invoice_billing"],
            attempt_count=1,
            react_trace=None,
        )
    )

    monkeypatch.setattr(main.db, "create_pool", mock_create_pool)
    monkeypatch.setattr(main.db, "bootstrap", mock_bootstrap)
    monkeypatch.setattr(main.ingest, "ensure_hnsw_index", mock_ensure_hnsw)
    monkeypatch.setattr(main, "generate_sql", mock_generate_sql)

    response = await client.post(
        "/generate-sql",
        json={"query": "newest payment", "top_k": 3},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert app.state.pool is restored_pool
    assert mock_create_pool.await_count == 1
    assert mock_bootstrap.await_count == 1
    assert mock_ensure_hnsw.await_count == 1


@pytest.mark.asyncio
async def test_auto_reconnect_failure_returns_503(
    app,
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import main
    from nl2sql_service.config import settings

    app.state.pool = None
    app.state.pool_last_reconnect_attempt = 0.0
    settings.db_reconnect_min_interval = 0.0

    mock_create_pool = AsyncMock(side_effect=TimeoutError("DB unreachable"))
    monkeypatch.setattr(main.db, "create_pool", mock_create_pool)

    response = await client.post(
        "/generate-sql",
        json={"query": "newest payment", "top_k": 3},
    )

    assert response.status_code == 503
    assert "retry connection automatically" in response.json()["detail"]
    assert mock_create_pool.await_count == 1


@pytest.mark.asyncio
async def test_auto_reconnect_throttles_attempts(
    app,
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import main
    from nl2sql_service.config import settings

    app.state.pool = None
    app.state.pool_last_reconnect_attempt = time.monotonic()
    settings.db_reconnect_min_interval = 60.0

    mock_create_pool = AsyncMock(side_effect=AssertionError("Should not be called"))
    monkeypatch.setattr(main.db, "create_pool", mock_create_pool)

    response = await client.post(
        "/generate-sql",
        json={"query": "newest payment", "top_k": 3},
    )

    assert response.status_code == 503
    assert mock_create_pool.await_count == 0


@pytest.mark.asyncio
async def test_health_attempts_reconnect_when_pool_missing(
    app,
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import main
    from nl2sql_service.config import settings

    app.state.pool = None
    app.state.pool_last_reconnect_attempt = 0.0
    settings.db_reconnect_min_interval = 0.0

    restored_pool = AsyncMock()
    restored_pool.execute = AsyncMock(return_value="SELECT 1")
    monkeypatch.setattr(main.db, "create_pool", AsyncMock(return_value=restored_pool))
    monkeypatch.setattr(main.db, "bootstrap", AsyncMock(return_value=None))
    monkeypatch.setattr(main.ingest, "ensure_hnsw_index", AsyncMock(return_value=None))
    monkeypatch.setattr(
        main.mysql_executor,
        "mysql_target_readiness",
        AsyncMock(return_value={"status": "ok", "issues": []}),
    )
    monkeypatch.setattr(
        main.schema_loader,
        "loader_readiness",
        lambda: {"status": "ok", "issues": []},
    )

    response = await client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["db"] == "connected"
    assert payload["provider_config"]["status"] == "ok"
    assert payload["teach_confirmations"] == {"status": "unavailable", "alerts": []}
    assert app.state.pool is restored_pool
