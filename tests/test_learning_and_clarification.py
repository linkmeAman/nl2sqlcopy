from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nl2sql_service import pattern_store, react_agent, retrieve
from nl2sql_service.config import settings
from nl2sql_service.models import (
    GenerateSqlSuccess,
    ReActAction,
    SqlWarning,
    WarningCode,
)


class _Acquire:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def __aenter__(self) -> Any:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakePool:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


class _Transaction:
    async def __aenter__(self) -> "_Transaction":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _GroupConn:
    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        del sql, args
        return [
            {
                "content": "Group: inquiry lifecycle\nEmployee and contact context",
                "similarity": 0.95,
                "source": "inquiry_lifecycle",
                "chunk_index": 0,
                "token_count": 12,
                "embedding_model": "bge-large-en-v1.5",
                "metadata": {
                    "type": "schema_group",
                    "tables": ["employee"],
                    "related_tables": ["contact"],
                    "group_description": "Employee/contact search",
                },
            }
        ]


class _PatternConn:
    def __init__(self, patterns: list[dict]) -> None:
        self.patterns = patterns
        self.cache_epoch = 1

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        if "tables_used &&" in sql:
            tables_in_scope = set(args[0])
            min_use_count = args[1]
            limit = args[2]
            rows = [
                pattern
                for pattern in self.patterns
                if pattern.get("is_active", True)
                and pattern["use_count"] >= min_use_count
                and tables_in_scope.intersection(pattern["tables_used"])
            ]
            return rows[:limit]

        min_use_count = args[0]
        return [
            pattern
            for pattern in self.patterns
            if pattern.get("is_active", True)
            and pattern["use_count"] >= min_use_count
        ]

    async def fetchrow(self, sql: str, *args: Any) -> dict | None:
        if "UPDATE nl2sql_cache_state" in sql:
            self.cache_epoch += 1
            return {"cache_epoch": self.cache_epoch}

        pattern_id = args[0]
        for pattern in self.patterns:
            if pattern["id"] != pattern_id:
                continue
            if "use_count = use_count + 2" in sql:
                pattern["use_count"] += 2
            if "is_active = FALSE" in sql:
                pattern["is_active"] = False
            return {"id": pattern_id}
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        del args
        if "INSERT INTO nl2sql_cache_state" in sql:
            return "INSERT 0 1"
        return "OK"


@pytest.fixture(autouse=True)
def patch_react_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "react_max_iterations", 4)
    monkeypatch.setattr(
        react_agent,
        "load_columns_for_tables",
        AsyncMock(
            return_value={
                "invoice": ["id", "member_id", "amount", "status"],
                "member": ["id", "name"],
                "payment": ["id", "invoice_id", "amount"],
                "employee": ["id", "contact_id"],
                "contact": ["id", "name"],
            }
        ),
    )
    monkeypatch.setattr(react_agent, "run_explain", AsyncMock(return_value=[]))


def _pattern(pattern_id: int, use_count: int = 5, is_active: bool = True) -> dict:
    return {
        "id": pattern_id,
        "query_text": "fetch employee named aman",
        "sql_used": (
            "SELECT e.* FROM employee e JOIN contact c "
            "ON c.id = e.contact_id WHERE c.name LIKE '%aman%'"
        ),
        "tables_used": ["employee", "contact"],
        "join_conditions": [
            {
                "left_table": "employee",
                "left_column": "contact_id",
                "right_table": "contact",
                "right_column": "id",
                "join_type": "INNER",
            }
        ],
        "matched_groups": ["inquiry_lifecycle"],
        "use_count": use_count,
        "last_used_at": "2026-04-27T10:00:00",
        "created_at": "2026-04-27T10:00:00",
        "is_active": is_active,
    }


def test_extract_join_conditions_single_join() -> None:
    sql = "SELECT e.* FROM employee e JOIN contact c ON c.id = e.contact_id"

    result = pattern_store.extract_join_conditions(sql)

    assert len(result) == 1
    tables = {result[0]["left_table"], result[0]["right_table"]}
    assert tables == {"employee", "contact"}


def test_extract_join_conditions_no_join() -> None:
    sql = "SELECT * FROM inquiry ORDER BY created_at DESC"

    result = pattern_store.extract_join_conditions(sql)

    assert result == []


def test_extract_join_conditions_multiple_joins() -> None:
    sql = """
    SELECT * FROM invoice i
    JOIN member m ON m.id = i.member_id
    JOIN payment p ON p.invoice_id = i.id
    """

    result = pattern_store.extract_join_conditions(sql)

    assert len(result) == 2


@pytest.mark.asyncio
async def test_patterns_injected_into_context(
    mock_embed,
    mock_pattern_store_with_join_pattern,
) -> None:
    del mock_embed, mock_pattern_store_with_join_pattern

    result = await retrieve.retrieve_groups(
        query="fetch employee named aman",
        top_k=3,
        pool=_FakePool(_GroupConn()),
    )

    assert "PREVIOUSLY LEARNED PATTERNS" in result.context
    assert "employee" in result.context
    assert "contact" in result.context


@pytest.mark.asyncio
async def test_no_patterns_context_unchanged(
    mock_embed,
    mock_pattern_store_empty,
) -> None:
    del mock_embed, mock_pattern_store_empty

    result = await retrieve.retrieve_groups(
        query="fetch employee named aman",
        top_k=3,
        pool=_FakePool(_GroupConn()),
    )

    assert "PREVIOUSLY LEARNED PATTERNS" not in result.context


@pytest.mark.asyncio
async def test_give_up_returns_clarification_not_rejection(
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_build_clarification,
) -> None:
    del mock_react_retrieve, mock_build_clarification
    mock_call_reasoning_model.return_value = (
        "The schema context is insufficient",
        "ACTION: GIVE_UP\nINPUT: GIVE_UP",
        [],
    )

    response = await react_agent.run(
        query="fetch aman",
        pool=object(),
        settings=settings,
    )

    assert response.status == "clarification_needed"
    assert response.question
    assert len(response.suggestions) >= 2
    assert response.original_query == "fetch aman"


@pytest.mark.asyncio
async def test_max_retries_exceeded_returns_clarification(
    monkeypatch: pytest.MonkeyPatch,
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_react_call_ollama,
    mock_build_clarification,
) -> None:
    del mock_react_retrieve, mock_build_clarification
    monkeypatch.setattr(settings, "react_max_iterations", 2)
    mock_call_reasoning_model.return_value = (
        "Generate SQL",
        "ACTION: GENERATE_SQL\nINPUT: generate select",
        [],
    )
    mock_react_call_ollama.return_value = ("SELECT * FROM forbidden_table", [])

    response = await react_agent.run(
        query="show forbidden data",
        pool=object(),
        settings=settings,
    )

    assert response.status == "clarification_needed"
    assert response.status != "rejected"


@pytest.mark.asyncio
async def test_ollama_timeout_returns_rejected_not_clarification(
    mock_call_reasoning_model,
    mock_react_retrieve,
) -> None:
    del mock_react_retrieve
    mock_call_reasoning_model.return_value = (
        "",
        "",
        [
            SqlWarning(
                code=WarningCode.OLLAMA_TIMEOUT,
                message="Reasoning model timed out",
            )
        ],
    )

    response = await react_agent.run(
        query="show invoices",
        pool=object(),
        settings=settings,
    )

    assert response.status == "rejected"
    assert any(warning.code == WarningCode.OLLAMA_TIMEOUT for warning in response.warnings)


@pytest.mark.asyncio
async def test_generate_sql_give_up_returns_clarification(
    client,
    mock_call_reasoning_model,
    mock_retrieve_groups,
    mock_build_clarification,
) -> None:
    del mock_retrieve_groups, mock_build_clarification
    mock_call_reasoning_model.return_value = (
        "No relevant schema",
        "ACTION: GIVE_UP\nINPUT: Cannot determine table",
        [],
    )

    response = await client.post(
        "/generate-sql",
        json={"query": "fetch aman"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "clarification_needed"
    assert "question" in body
    assert "suggestions" in body
    assert "sql" not in body


@pytest.mark.asyncio
async def test_ask_give_up_returns_clarification_without_execution(
    client,
    mock_call_reasoning_model,
    mock_retrieve_groups,
    mock_build_clarification,
    mock_ask_execute_sql,
) -> None:
    del mock_retrieve_groups, mock_build_clarification
    mock_call_reasoning_model.return_value = (
        "No relevant schema",
        "ACTION: GIVE_UP\nINPUT: Cannot determine table",
        [],
    )

    response = await client.post(
        "/ask",
        json={"query": "fetch aman"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "clarification_needed"
    assert mock_ask_execute_sql.await_count == 0


@pytest.mark.asyncio
async def test_save_pattern_called_when_row_count_positive(
    client,
    monkeypatch: pytest.MonkeyPatch,
    mock_ask_answer_generator,
    mock_save_pattern,
) -> None:
    from nl2sql_service import main, mysql_executor

    monkeypatch.setattr(
        main,
        "generate_sql",
        AsyncMock(
            return_value=GenerateSqlSuccess(
                sql="SELECT i.* FROM invoice i JOIN member m ON m.id = i.member_id",
                warnings=[],
                tables_used=["invoice", "member"],
                matched_groups=["billing"],
                attempt_count=1,
                react_trace=None,
            )
        ),
    )
    monkeypatch.setattr(
        mysql_executor,
        "execute_sql",
        AsyncMock(return_value=(["id"], [(1,), (2,), (3,)], [])),
    )

    response = await client.post("/ask", json={"query": "show invoices"})
    await asyncio.sleep(0)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert mock_ask_answer_generator.await_count == 1
    assert mock_save_pattern.call_count == 1
    assert mock_save_pattern.call_args.kwargs["tables_used"]


@pytest.mark.asyncio
async def test_save_pattern_not_called_when_row_count_zero(
    client,
    monkeypatch: pytest.MonkeyPatch,
    mock_ask_answer_generator,
    mock_save_pattern,
) -> None:
    from nl2sql_service import main, mysql_executor

    monkeypatch.setattr(
        main,
        "generate_sql",
        AsyncMock(
            return_value=GenerateSqlSuccess(
                sql="SELECT id FROM invoice",
                warnings=[],
                tables_used=["invoice"],
                matched_groups=["billing"],
                attempt_count=1,
                react_trace=None,
            )
        ),
    )
    monkeypatch.setattr(
        mysql_executor,
        "execute_sql",
        AsyncMock(return_value=(["id"], [], [])),
    )

    response = await client.post("/ask", json={"query": "show invoices"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert mock_ask_answer_generator.await_count == 1
    assert mock_save_pattern.call_count == 0


@pytest.mark.asyncio
async def test_ingest_patterns_embeds_active_patterns(
    app,
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import main

    patterns = [_pattern(1), _pattern(2)]
    app.state.pool = _FakePool(_PatternConn(patterns))

    async def fake_upsert(chunks: list[dict], pool: object) -> dict[str, int]:
        del pool
        return {"inserted_count": len(chunks), "updated_count": 0}

    monkeypatch.setattr(main.ingest, "_upsert_versioned_chunks", AsyncMock(side_effect=fake_upsert))

    response = await client.post("/ingest/patterns")

    body = response.json()
    assert response.status_code == 200
    assert body["embedded"] >= 2
    assert body["source"] == "learned_patterns"


@pytest.mark.asyncio
async def test_patterns_feedback_helpful_boosts(app, client) -> None:
    pattern = _pattern(1, use_count=5)
    app.state.pool = _FakePool(_PatternConn([pattern]))

    response = await client.post(
        "/patterns/feedback",
        json={"pattern_id": 1, "helpful": True},
    )

    assert response.status_code == 200
    assert response.json() == {"pattern_id": 1, "action": "boosted"}
    assert pattern["use_count"] == 7


@pytest.mark.asyncio
async def test_patterns_feedback_false_deactivates(app, client) -> None:
    pattern = _pattern(1, use_count=5)
    pool = _FakePool(_PatternConn([pattern]))
    app.state.pool = pool

    response = await client.post(
        "/patterns/feedback",
        json={"pattern_id": 1, "helpful": False},
    )
    patterns = await pattern_store.get_relevant_patterns(
        query="fetch employee named aman",
        tables_in_scope=["employee", "contact"],
        pool=pool,
    )

    assert response.status_code == 200
    assert response.json() == {"pattern_id": 1, "action": "deactivated"}
    assert pattern["is_active"] is False
    assert patterns == []
