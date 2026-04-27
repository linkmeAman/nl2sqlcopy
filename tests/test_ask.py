from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from nl2sql_service.models import (
    GenerateSqlRejected,
    GenerateSqlSuccess,
    SqlWarning,
    WarningCode,
)
from nl2sql_service.mysql_executor import apply_row_cap


@pytest.mark.asyncio
async def test_ask_success_full_response(
    client,
    monkeypatch: pytest.MonkeyPatch,
    mock_ask_execute_sql,
    mock_ask_answer_generator,
):
    from nl2sql_service import main

    mock_generate_sql = AsyncMock(
        return_value=GenerateSqlSuccess(
            sql="SELECT id, amount FROM payment ORDER BY date DESC",
            warnings=[],
            tables_used=["payment"],
            matched_groups=["sales_invoice_billing"],
            attempt_count=2,
            react_trace=None,
        )
    )
    monkeypatch.setattr(main, "generate_sql", mock_generate_sql)

    response = await client.post(
        "/ask",
        json={"query": "newest payment", "top_k": 5},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["answer"] == "There are 2 matching rows."
    assert body["sql"].endswith("LIMIT 50")
    assert body["row_count"] == 2
    assert body["columns"] == ["id", "amount"]
    assert body["tables_used"] == ["payment"]
    assert body["matched_groups"] == ["sales_invoice_billing"]
    assert body["attempt_count"] == 2
    assert "react_trace" in body

    assert mock_generate_sql.await_count == 1
    assert mock_ask_execute_sql.await_count == 1
    assert mock_ask_answer_generator.await_count == 1


@pytest.mark.asyncio
async def test_ask_rejected_sql_generation_skips_execution(
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import answer_generator, main, mysql_executor

    mock_generate_sql = AsyncMock(
        return_value=GenerateSqlRejected(
            warnings=[
                SqlWarning(
                    code=WarningCode.MAX_RETRIES_EXCEEDED,
                    message="ReAct loop exhausted",
                )
            ],
            attempt_count=4,
            react_trace=None,
        )
    )
    mock_execute_sql = AsyncMock(return_value=([], [], []))
    mock_generate_answer = AsyncMock(return_value=("unused", []))

    monkeypatch.setattr(main, "generate_sql", mock_generate_sql)
    monkeypatch.setattr(mysql_executor, "execute_sql", mock_execute_sql)
    monkeypatch.setattr(answer_generator, "generate_answer", mock_generate_answer)

    response = await client.post(
        "/ask",
        json={"query": "ambiguous", "top_k": 5},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "rejected"
    assert body["answer"] is None
    assert body["sql"] is None
    assert body["attempt_count"] == 4
    assert mock_execute_sql.await_count == 0
    assert mock_generate_answer.await_count == 0


@pytest.mark.asyncio
async def test_ask_mysql_execution_failure_returns_controlled_rejection(
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import main, mysql_executor

    mock_generate_sql = AsyncMock(
        return_value=GenerateSqlSuccess(
            sql="SELECT id FROM payment",
            warnings=[],
            tables_used=["payment"],
            matched_groups=["sales_invoice_billing"],
            attempt_count=1,
            react_trace=None,
        )
    )
    mock_execute_sql = AsyncMock(
        return_value=(
            [],
            [],
            [
                SqlWarning(
                    code=WarningCode.MYSQL_QUERY_ERROR,
                    message="MySQL query failed: access denied",
                )
            ],
        )
    )

    monkeypatch.setattr(main, "generate_sql", mock_generate_sql)
    monkeypatch.setattr(mysql_executor, "execute_sql", mock_execute_sql)

    response = await client.post(
        "/ask",
        json={"query": "newest payment", "top_k": 5},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "rejected"
    assert body["sql"].startswith("SELECT id FROM payment")
    assert any(
        warning["code"] == WarningCode.MYSQL_QUERY_ERROR.value
        for warning in body["warnings"]
    )


@pytest.mark.asyncio
async def test_ask_answer_llm_timeout_returns_controlled_rejection(
    client,
    monkeypatch: pytest.MonkeyPatch,
    mock_ask_execute_sql,
):
    from nl2sql_service import answer_generator, main

    mock_generate_sql = AsyncMock(
        return_value=GenerateSqlSuccess(
            sql="SELECT id FROM payment",
            warnings=[],
            tables_used=["payment"],
            matched_groups=["sales_invoice_billing"],
            attempt_count=1,
            react_trace=None,
        )
    )
    mock_generate_answer = AsyncMock(
        return_value=(
            None,
            [
                SqlWarning(
                    code=WarningCode.ANSWER_TIMEOUT,
                    message="Answer model timed out",
                )
            ],
        )
    )

    monkeypatch.setattr(main, "generate_sql", mock_generate_sql)
    monkeypatch.setattr(answer_generator, "generate_answer", mock_generate_answer)

    response = await client.post(
        "/ask",
        json={"query": "newest payment", "top_k": 5},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "rejected"
    assert body["sql"].startswith("SELECT id FROM payment")
    assert any(
        warning["code"] == WarningCode.ANSWER_TIMEOUT.value
        for warning in body["warnings"]
    )
    assert any("Execution metadata:" in warning["message"] for warning in body["warnings"])


@pytest.mark.asyncio
async def test_ask_success_response_includes_required_fields(
    client,
    monkeypatch: pytest.MonkeyPatch,
    mock_ask_execute_sql,
    mock_ask_answer_generator,
):
    from nl2sql_service import main

    mock_generate_sql = AsyncMock(
        return_value=GenerateSqlSuccess(
            sql="SELECT id FROM payment",
            warnings=[],
            tables_used=["payment"],
            matched_groups=["sales_invoice_billing"],
            attempt_count=1,
            react_trace=None,
        )
    )
    monkeypatch.setattr(main, "generate_sql", mock_generate_sql)

    response = await client.post(
        "/ask",
        json={"query": "newest payment", "top_k": 5},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert set(body.keys()) == {
        "status",
        "answer",
        "sql",
        "warnings",
        "row_count",
        "columns",
        "tables_used",
        "matched_groups",
        "attempt_count",
        "react_trace",
    }


def test_ask_sql_without_limit_is_capped_to_50():
    sql = "SELECT id, amount FROM payment ORDER BY date DESC"
    capped = apply_row_cap(sql, cap=50)
    assert capped.endswith("LIMIT 50")


def test_ask_sql_with_smaller_limit_is_preserved():
    sql = "SELECT id FROM payment ORDER BY date DESC LIMIT 5"
    capped = apply_row_cap(sql, cap=50)
    assert capped == sql


def test_ask_sql_with_larger_limit_is_capped():
    sql = "SELECT id FROM payment ORDER BY date DESC LIMIT 200"
    capped = apply_row_cap(sql, cap=50)
    assert capped.endswith("LIMIT 50")


@pytest.mark.asyncio
async def test_ask_stream_success_emits_progress_and_final(
    client,
    monkeypatch: pytest.MonkeyPatch,
    mock_ask_execute_sql,
    mock_ask_answer_generator,
):
    from nl2sql_service import main

    mock_generate_sql = AsyncMock(
        return_value=GenerateSqlSuccess(
            sql="SELECT id, amount FROM payment ORDER BY date DESC",
            warnings=[],
            tables_used=["payment"],
            matched_groups=["sales_invoice_billing"],
            attempt_count=2,
            react_trace=None,
        )
    )
    monkeypatch.setattr(main, "generate_sql", mock_generate_sql)

    response = await client.post(
        "/ask/stream",
        json={"query": "newest payment", "top_k": 5},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")

    events = [json.loads(line) for line in response.text.splitlines()]
    event_names = [event["event"] for event in events]
    assert event_names == [
        "started",
        "sql_generation_started",
        "sql_generation_finished",
        "row_cap_applied",
        "execution_started",
        "execution_finished",
        "answer_generation_started",
        "answer_generation_finished",
        "final",
    ]
    assert events[2]["sql"] == "SELECT id, amount FROM payment ORDER BY date DESC"
    assert events[5]["row_count"] == 2
    assert events[-1]["response"]["status"] == "ok"
    assert events[-1]["response"]["answer"] == "There are 2 matching rows."


@pytest.mark.asyncio
async def test_ask_stream_rejected_sql_generation_returns_final_event(
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import main

    mock_generate_sql = AsyncMock(
        return_value=GenerateSqlRejected(
            warnings=[
                SqlWarning(
                    code=WarningCode.MAX_RETRIES_EXCEEDED,
                    message="ReAct loop exhausted",
                )
            ],
            attempt_count=4,
            react_trace=None,
        )
    )
    monkeypatch.setattr(main, "generate_sql", mock_generate_sql)

    response = await client.post(
        "/ask/stream",
        json={"query": "ambiguous", "top_k": 5},
    )

    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["event"] for event in events] == [
        "started",
        "sql_generation_started",
        "sql_generation_rejected",
        "final",
    ]
    assert events[-1]["response"]["status"] == "rejected"
