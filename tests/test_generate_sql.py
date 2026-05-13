from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nl2sql_service import sql_generator
from nl2sql_service.models import SqlWarning, WarningCode


@pytest.mark.asyncio
async def test_valid_select_status_ok(client, mock_ollama, mock_retrieve_groups):
    mock_ollama.return_value = (
        "SELECT id, amount FROM invoice WHERE status='unpaid'",
        [],
    )

    response = await client.post(
        "/generate-sql",
        json={"query": "show unpaid invoices"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["sql"] is not None
    assert body["attempt_count"] == 1
    assert body["cache_hit"] is False
    assert body["react_trace"]["total_iterations"] == 1
    assert body["react_trace"]["final_action"] == "VALIDATE_AND_RETURN"
    assert "invoice" in body["tables_used"]


@pytest.mark.asyncio
async def test_valid_with_select_status_ok(client, mock_ollama, mock_retrieve_groups):
    mock_ollama.return_value = (
        "WITH unpaid AS (SELECT * FROM invoice WHERE status='unpaid')\n"
        "SELECT m.name, u.total FROM member m\n"
        "JOIN unpaid u ON m.id = u.member_id",
        [],
    )

    response = await client.post(
        "/generate-sql",
        json={"query": "show unpaid members"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert "unpaid" not in body["tables_used"]


def test_sql_prompts_use_concise_column_selection_rule():
    prompt = sql_generator.build_sql_prompt(
        query="show recent inquiries",
        context="Group: inquiry_lifecycle",
        tables_in_scope=["inquiry"],
        allowed_columns={"inquiry": ["id", "contact_id", "created_at"]},
        dialect="mysql",
    )

    assert "choose concise, semantically relevant columns" in prompt
    assert "COLUMN SELECTION RULE:" in prompt
    assert "Do NOT select: financial columns" in prompt
    assert "Use SELECT * only when the user explicitly asks" in prompt
    assert "Do not use SELECT *" not in prompt


def test_refinement_prompt_uses_column_selection_rule():
    prompt = sql_generator.build_refinement_prompt(
        query="show recent inquiries",
        context="Group: inquiry_lifecycle",
        tables_in_scope=["inquiry"],
        dialect="mysql",
        previous_sql="SELECT * FROM inquiry",
        validation_errors=[],
        attempt=1,
    )

    assert "COLUMN SELECTION RULE:" in prompt
    assert "SELECT only the columns needed for the calculation" in prompt


def test_inquiry_select_star_is_narrowed_without_low_signal_columns():
    columns = [
        "id",
        "contact_id",
        "type",
        "employee_id",
        "allocation_date",
        "source",
        "heard_from",
        "converted",
        "last_updated",
        "balance",
        "created_by",
        "created_at",
    ]

    sql = sql_generator.narrow_select_star(
        "SELECT * FROM inquiry ORDER BY created_at DESC LIMIT 5;",
        {"inquiry": columns},
        "show me the 5 most recent inquiries",
    )

    assert "*" not in sql
    assert "id" in sql
    assert "contact_id" in sql
    assert "created_at" in sql
    assert "balance" not in sql
    assert "created_by" not in sql
    assert "last_updated" not in sql


def test_select_star_is_preserved_when_user_asks_for_full_details():
    sql = sql_generator.narrow_select_star(
        "SELECT * FROM inquiry ORDER BY created_at DESC LIMIT 5;",
        {"inquiry": ["id", "contact_id", "created_at"]},
        "show full details for the 5 most recent inquiries",
    )

    assert sql.startswith("SELECT * FROM inquiry")


@pytest.mark.asyncio
async def test_leading_comment_valid_select(client, mock_ollama, mock_retrieve_groups):
    mock_ollama.return_value = (
        "-- Assuming status values are: unpaid, paid\n"
        "SELECT id FROM invoice WHERE status='unpaid'",
        [],
    )

    response = await client.post(
        "/generate-sql",
        json={"query": "show unpaid invoice ids"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["sql"].startswith("--")


@pytest.mark.asyncio
async def test_destructive_sql_returns_clarification(
    client,
    mock_ollama,
    mock_retrieve_groups,
    mock_build_clarification,
):
    del mock_build_clarification
    mock_ollama.return_value = ("DROP TABLE invoice", [])

    response = await client.post(
        "/generate-sql",
        json={"query": "remove invoice table"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "clarification_needed"
    assert WarningCode.SQL_DESTRUCTIVE.value in body["failure_reason"]
    assert "sql" not in body
    assert "tables_used" not in body


@pytest.mark.asyncio
async def test_multi_statement_returns_clarification(
    client,
    mock_ollama,
    mock_retrieve_groups,
    mock_build_clarification,
):
    del mock_build_clarification
    mock_ollama.return_value = ("SELECT * FROM invoice; DROP TABLE invoice", [])

    response = await client.post(
        "/generate-sql",
        json={"query": "show invoice and drop table"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "clarification_needed"
    assert WarningCode.SQL_MULTI_STATEMENT.value in body["failure_reason"]


@pytest.mark.asyncio
async def test_unknown_table_self_corrects(client, mock_ollama, mock_retrieve_groups):
    mock_ollama.side_effect = [
        ("SELECT * FROM forbidden_table", []),
        ("SELECT * FROM invoice WHERE status='unpaid'", []),
    ]

    response = await client.post(
        "/generate-sql",
        json={"query": "show unpaid invoices"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["attempt_count"] == 2
    assert mock_ollama.call_count == 2


@pytest.mark.asyncio
async def test_all_attempts_fail_returns_clarification(
    client,
    mock_ollama,
    mock_retrieve_groups,
    mock_build_clarification,
):
    del mock_build_clarification
    mock_ollama.return_value = ("SELECT * FROM forbidden_table", [])

    response = await client.post(
        "/generate-sql",
        json={"query": "show forbidden data"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "clarification_needed"
    assert WarningCode.MAX_RETRIES_EXCEEDED.value in body["failure_reason"]
    assert WarningCode.TABLE_OUT_OF_SCOPE.value in body["failure_reason"]
    assert body["react_trace"]["total_iterations"] == 4


@pytest.mark.asyncio
async def test_clarification_trace_after_validation_driven_retry(
    monkeypatch: pytest.MonkeyPatch,
    client,
    mock_ollama,
    mock_retrieve_groups,
    mock_build_clarification,
):
    del mock_build_clarification
    from nl2sql_service.config import settings

    monkeypatch.setattr(settings, "react_max_iterations", 2)
    mock_ollama.side_effect = [
        ("SELECT * FROM forbidden_table", []),
        ("SELECT * FROM forbidden_table", []),
    ]

    response = await client.post(
        "/generate-sql",
        json={"query": "show unpaid invoices"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "clarification_needed"
    assert "sql" not in body

    trace = body["react_trace"]
    assert trace["total_iterations"] == 2
    assert trace["final_action"] == "VALIDATE_AND_RETURN"

    actions = [step["action"] for step in trace["steps"]]
    assert actions == [
        "GENERATE_SQL",
        "GENERATE_SQL",
    ]
    assert "Auto-validation: FAILED:" in trace["steps"][0]["observation"]
    assert "Auto-validation: FAILED:" in trace["steps"][1]["observation"]

    assert WarningCode.TABLE_OUT_OF_SCOPE.value in body["failure_reason"]
    assert WarningCode.MAX_RETRIES_EXCEEDED.value in body["failure_reason"]


@pytest.mark.asyncio
async def test_ollama_timeout_status_rejected_http_200(
    client,
    mock_ollama,
    mock_retrieve_groups,
):
    mock_ollama.return_value = (
        None,
        [
            SqlWarning(
                code=WarningCode.OLLAMA_TIMEOUT,
                message="Ollama request timed out after 60s",
            )
        ],
    )

    response = await client.post(
        "/generate-sql",
        json={"query": "show unpaid invoices"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "rejected"
    assert any(
        warning["code"] == WarningCode.OLLAMA_TIMEOUT.value
        for warning in body["warnings"]
    )


@pytest.mark.asyncio
async def test_clarification_payload_shape(
    client,
    mock_ollama,
    mock_retrieve_groups,
    mock_build_clarification,
):
    del mock_build_clarification
    mock_ollama.return_value = ("DROP TABLE invoice", [])

    response = await client.post(
        "/generate-sql",
        json={"query": "remove invoice table"},
    )

    body = response.json()
    assert response.status_code == 200
    assert set(body.keys()) == {
        "status",
        "question",
        "suggestions",
        "original_query",
        "failure_reason",
        "cache_hit",
        "react_trace",
    }
    assert body["status"] == "clarification_needed"
    assert "sql" not in body
    assert "tables_used" not in body
    assert "matched_groups" not in body


@pytest.mark.asyncio
async def test_db_unavailable_returns_503(
    app,
    client,
    monkeypatch: pytest.MonkeyPatch,
    mock_ollama,
    mock_retrieve_groups,
):
    from nl2sql_service import main

    app.state.pool = None
    app.state.pool_last_reconnect_attempt = 0.0
    monkeypatch.setattr(main.settings, "db_reconnect_min_interval", 0.0)
    monkeypatch.setattr(
        main.db,
        "create_pool",
        AsyncMock(side_effect=TimeoutError("DB unavailable")),
    )

    response = await client.post(
        "/generate-sql",
        json={"query": "show unpaid invoices"},
    )

    assert response.status_code == 503
