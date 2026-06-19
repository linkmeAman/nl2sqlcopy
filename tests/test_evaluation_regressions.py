from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nl2sql_service.agent import react_agent, react_executor, react_planner
from nl2sql_service.generation import sql_generator
from nl2sql_service.models import QueryResult, WarningCode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "table_name", "columns"),
    [
        (
            "show me the 5 most recent payments",
            "payment",
            ["id", "invoice_id", "amount", "created_at"],
        ),
        (
            "show me the 5 most recent inquiries",
            "inquiry",
            ["id", "contact_id", "created_at", "source"],
        ),
        (
            "list active members",
            "member",
            ["id", "name", "status", "created_at"],
        ),
        (
            "find contact by mobile number",
            "contact",
            ["id", "fname", "lname", "mobile", "created_at"],
        ),
    ],
)
async def test_level1_queries_fail_fast_with_schema_in_scope(
    client,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    table_name: str,
    columns: list[str],
) -> None:
    monkeypatch.setattr(
        sql_generator.retrieve,
        "retrieve_groups",
        AsyncMock(
            return_value={
                "matched_groups": [f"{table_name}_group"],
                "tables_in_scope": [table_name],
                "context": f"Group: {table_name}_group\nTables: {table_name}",
                "results": [],
            }
        ),
    )
    monkeypatch.setattr(
        sql_generator,
        "load_columns_for_tables",
        AsyncMock(return_value={table_name: columns}),
    )
    monkeypatch.setattr(sql_generator, "run_explain", AsyncMock(return_value=[]))
    react_run = AsyncMock(side_effect=AssertionError("ReAct should not run for level-1 fast-path cases"))
    monkeypatch.setattr(react_executor, "run", react_run)

    response = await client.post("/generate-sql", json={"query": query})

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert f"FROM {table_name}" in body["sql"]
    assert body["tables_used"] == [table_name]
    assert body["attempt_count"] == 0
    assert react_run.await_count == 0
    assert not any(
        warning["code"] == WarningCode.REQUEST_TIMEOUT.value
        for warning in body.get("warnings", [])
    )


@pytest.mark.asyncio
async def test_destructive_evaluation_case_rejects_before_react(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    react_run = AsyncMock(side_effect=AssertionError("ReAct should not run for destructive preflight rejections"))
    monkeypatch.setattr(react_executor, "run", react_run)

    response = await client.post(
        "/generate-sql",
        json={"query": "delete old payments from last year"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "rejected"
    assert body["attempt_count"] == 0
    assert react_run.await_count == 0
    assert any(
        warning["code"] == WarningCode.SQL_DESTRUCTIVE.value
        for warning in body["warnings"]
    )
    assert not any(
        warning["code"] == WarningCode.REQUEST_TIMEOUT.value
        for warning in body["warnings"]
    )


@pytest.mark.asyncio
async def test_ambiguous_evaluation_case_clarifies_before_react(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    react_run = AsyncMock(side_effect=AssertionError("ReAct should not run for basic ambiguity preflight"))
    monkeypatch.setattr(react_executor, "run", react_run)

    response = await client.post(
        "/generate-sql",
        json={"query": "show active records"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "clarification_needed"
    assert body["react_trace"]["total_iterations"] == 0
    assert react_run.await_count == 0
    assert "target entity" in body["failure_reason"]


@pytest.mark.asyncio
async def test_bootstrap_schema_focuses_payment_columns_for_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        react_executor,
        "retrieve_groups",
        AsyncMock(
            return_value={
                "matched_groups": ["billing"],
                "tables_in_scope": ["invoice", "member", "payment"],
                "context": "Group: billing\nTables: invoice, member, payment",
                "results": [],
            }
        ),
    )
    monkeypatch.setattr(
        react_executor,
        "retrieve_column_catalog",
        AsyncMock(
            return_value=[
                QueryResult(
                    content="Table: payment\nColumn: id",
                    similarity=0.99,
                    metadata={"table_name": "payment", "column_name": "id"},
                ),
                QueryResult(
                    content="Table: payment\nColumn: created_at",
                    similarity=0.98,
                    metadata={"table_name": "payment", "column_name": "created_at"},
                ),
            ]
        ),
    )

    state: dict[str, object] = {"search_query": "show me the 5 most recent payments", "top_k": 5}
    observation, warnings = await react_executor.execute_action(
        action=react_agent.ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
        action_input="show me the 5 most recent payments",
        query="show me the 5 most recent payments",
        pool=object(),
        settings=__import__("nl2sql_service.core.config", fromlist=["settings"]).settings,
        state=state,
    )

    assert warnings == []
    assert "Focus tables: payment" in observation
    assert state["focus_tables"] == ["payment"]
    assert "payment" in state["retrieved_schema"]
