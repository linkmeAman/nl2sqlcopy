from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nl2sql_service import cache, retrieve, sql_generator
from nl2sql_service.models import CacheSource, GenerateSqlSuccess


def test_embed_cache_expires_by_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    local_cache = cache.EmbedCache()
    local_cache.clear()
    monkeypatch.setattr(cache, "EMBED_CACHE_TTL", 10)
    monkeypatch.setattr(cache.time, "time", lambda: 100.0)
    local_cache.set("hello", [1.0])

    monkeypatch.setattr(cache.time, "time", lambda: 105.0)
    assert local_cache.get("hello") == [1.0]

    monkeypatch.setattr(cache.time, "time", lambda: 111.0)
    assert local_cache.get("hello") is None


def test_embed_cache_evicts_oldest_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    local_cache = cache.EmbedCache()
    local_cache.clear()
    local_cache._max_size = 2
    now = [100.0]
    monkeypatch.setattr(cache.time, "time", lambda: now[0])

    local_cache.set("old", [1.0])
    now[0] = 101.0
    local_cache.set("middle", [2.0])
    now[0] = 102.0
    local_cache.set("new", [3.0])
    now[0] = 103.0

    assert local_cache.get("old") is None
    assert local_cache.get("middle") == [2.0]
    assert local_cache.get("new") == [3.0]


def test_sql_cache_only_caches_ok_results() -> None:
    local_cache = cache.SqlCache()
    local_cache.invalidate_all()

    local_cache.set("show invoices", 5, {"status": "rejected"})
    assert local_cache.get("show invoices", 5) is None

    local_cache.set("show invoices", 5, {"status": "ok", "sql": "SELECT 1"})
    assert local_cache.get(" SHOW INVOICES ", 5) == {"status": "ok", "sql": "SELECT 1"}


def test_sql_cache_uses_canonical_exact_key() -> None:
    local_cache = cache.SqlCache()
    local_cache.invalidate_all()

    local_cache.set("latest payment", 5, {"status": "ok", "sql": "SELECT 1"})

    assert local_cache.get("newest payment", 5) == {"status": "ok", "sql": "SELECT 1"}


def test_ask_cache_uses_canonical_exact_key() -> None:
    local_cache = cache.AskCache()
    local_cache.invalidate_all()

    payload = {"status": "ok", "answer": "cached"}
    local_cache.set("What is the latest payment?", 5, payload)

    assert local_cache.get_exact("show newest payment", 5) == {"status": "ok", "answer": "cached", "_top_k": 5}


@pytest.mark.asyncio
async def test_retrieve_uses_embed_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Acquire:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def fetch(self, sql: str, *args):
            del sql, args
            return []

    class _Pool:
        def acquire(self) -> _Acquire:
            return _Acquire()

    embed_mock = AsyncMock(return_value=[[0.1] * 1024])
    monkeypatch.setattr(retrieve.embed, "embed_texts", embed_mock)

    await retrieve.retrieve("newest payment", 5, _Pool())
    await retrieve.retrieve("newest payment", 5, _Pool())

    assert embed_mock.await_count == 1


@pytest.mark.asyncio
async def test_generate_sql_returns_cache_hit_on_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import react_agent
    from nl2sql_service.config import settings

    react_run = AsyncMock(
        return_value=GenerateSqlSuccess(
            sql="SELECT id FROM invoice",
            warnings=[],
            tables_used=["invoice"],
            matched_groups=["billing"],
            attempt_count=1,
            react_trace=None,
        )
    )
    monkeypatch.setattr(react_agent, "run", react_run)

    first = await sql_generator.generate_sql("show invoices", object(), settings, top_k=5)
    second = await sql_generator.generate_sql("show invoices", object(), settings, top_k=5)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.sql == "SELECT id FROM invoice"
    assert react_run.await_count == 1


@pytest.mark.asyncio
async def test_cache_ops_endpoints(client) -> None:
    cache.embed_cache.set("hello", [1.0])
    cache.sql_cache.set("show invoices", 5, {"status": "ok", "sql": "SELECT 1"})

    stats_response = await client.get("/cache/stats")
    assert stats_response.status_code == 200
    assert stats_response.json()["embed_cache_size"] == 1
    assert stats_response.json()["sql_cache_size"] == 1

    clear_response = await client.post("/cache/clear")
    assert clear_response.status_code == 200
    body = clear_response.json()
    assert body["embed_cleared"] == 1
    assert body["sql_cleared"] == 1
    assert {"semantic_sql_cleared", "ask_cleared", "db_query_cache_cleared"}.issubset(
        body.keys()
    )


@pytest.mark.asyncio
async def test_ask_semantic_cache_hits_for_deterministic_candidate(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import main

    embedding = [0.1] * 1024
    ask_workflow = AsyncMock()

    monkeypatch.setattr(main, "_load_query_embedding", AsyncMock(return_value=embedding))
    monkeypatch.setattr(main, "is_deterministic_generation_candidate", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "_run_ask_workflow", ask_workflow)

    cache.ask_cache.set(
        "latest payment",
        5,
        {
            "status": "ok",
            "answer": "Cached latest payment answer",
            "sql": "SELECT order_reference_no FROM payment ORDER BY date DESC LIMIT 1;",
            "warnings": [],
            "row_count": 1,
            "columns": ["order_reference_no"],
            "tables_used": ["payment"],
            "matched_groups": ["deterministic_payment"],
            "attempt_count": 1,
            "cache_hit": False,
            "cache_source": CacheSource.NONE.value,
            "react_trace": None,
            "stage_latencies_ms": {"sql_generation": 10, "execution": 1, "answer_generation": 1},
            "review_prompt": None,
        },
        embedding=embedding,
    )

    response = await client.post("/ask", json={"query": "most recent payment", "top_k": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["answer"] == "Cached latest payment answer"
    assert body["cache_hit"] is True
    assert body["cache_source"] == CacheSource.MEMORY_SEMANTIC.value
    assert ask_workflow.await_count == 0


@pytest.mark.asyncio
async def test_ask_exact_cache_hits_for_canonical_variant(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import main

    ask_workflow = AsyncMock()
    monkeypatch.setattr(main, "_run_ask_workflow", ask_workflow)

    cache.ask_cache.set(
        "latest payment",
        5,
        {
            "status": "ok",
            "answer": "Cached latest payment answer",
            "sql": "SELECT order_reference_no FROM payment ORDER BY date DESC LIMIT 1;",
            "warnings": [],
            "row_count": 1,
            "columns": ["order_reference_no"],
            "tables_used": ["payment"],
            "matched_groups": ["deterministic_payment"],
            "attempt_count": 1,
            "cache_hit": False,
            "cache_source": CacheSource.NONE.value,
            "react_trace": None,
            "stage_latencies_ms": {"sql_generation": 10, "execution": 1, "answer_generation": 1},
            "review_prompt": None,
        },
    )

    response = await client.post("/ask", json={"query": "newest payment", "top_k": 5})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["answer"] == "Cached latest payment answer"
    assert body["cache_hit"] is True
    assert body["cache_source"] == CacheSource.MEMORY_EXACT.value
    assert ask_workflow.await_count == 0
