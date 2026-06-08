from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from nl2sql_service import react_agent
from nl2sql_service import sql_generator
from nl2sql_service.models import QueryResult, SqlWarning, WarningCode


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
    assert body["react_trace"]["total_iterations"] >= 1
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


@pytest.mark.asyncio
async def test_contact_name_query_uses_retrieved_columns_not_prompt_hardcoding(
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service.config import settings

    state: dict[str, object] = {}
    monkeypatch.setattr(
        react_agent,
        "retrieve_groups",
        AsyncMock(
            return_value={
                "matched_groups": ["contact_crm"],
                "tables_in_scope": ["contact"],
                "context": "Group: contact_crm\nTables: contact",
                "results": [],
            }
        ),
    )
    monkeypatch.setattr(
        react_agent,
        "retrieve_column_catalog",
        AsyncMock(
            return_value=[
                QueryResult(
                    content="Table: contact\nColumn: fname",
                    similarity=0.96,
                    metadata={"type": "column_catalog", "table_name": "contact", "column_name": "fname"},
                ),
                QueryResult(
                    content="Table: contact\nColumn: lname",
                    similarity=0.95,
                    metadata={"type": "column_catalog", "table_name": "contact", "column_name": "lname"},
                ),
                QueryResult(
                    content="Table: contact\nColumn: fullname",
                    similarity=0.94,
                    metadata={"type": "column_catalog", "table_name": "contact", "column_name": "fullname"},
                ),
                QueryResult(
                    content="Table: contact\nColumn: email",
                    similarity=0.93,
                    metadata={"type": "column_catalog", "table_name": "contact", "column_name": "email"},
                ),
                QueryResult(
                    content="Table: contact\nColumn: mobile",
                    similarity=0.92,
                    metadata={"type": "column_catalog", "table_name": "contact", "column_name": "mobile"},
                ),
            ]
        ),
    )
    call_ollama = AsyncMock(
        return_value=(
            "SELECT id, fname, lname, email, mobile FROM contact WHERE fname = 'aman'",
            [],
        )
    )
    monkeypatch.setattr(react_agent, "call_ollama", call_ollama)
    retrieval_observation, warnings = await react_agent.execute_action(
        action=react_agent.ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
        action_input="contact",
        query="fetch contact info with name aman",
        pool=object(),
        settings=settings,
        state=state,
    )
    generation_observation, generation_warnings = await react_agent.execute_action(
        action=react_agent.ReActAction.GENERATE_SQL,
        action_input="generate contact sql",
        query="fetch contact info with name aman",
        pool=object(),
        settings=settings,
        state=state,
    )
    prompt = call_ollama.await_args.kwargs["prompt"]

    assert warnings == []
    assert generation_warnings == []
    assert "Columns refreshed via column-level retrieval" in retrieval_observation
    assert "Generated:" in generation_observation
    assert state["allowed_columns"]["contact"] == ["fname", "lname", "fullname", "email", "mobile"]
    assert "Only use these known columns:" in prompt
    assert "- contact: fname, lname, fullname, email, mobile" in prompt
    assert "Query-to-column hints:" not in prompt


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


def test_deterministic_recent_payment_sql_uses_live_columns():
    built = sql_generator.build_deterministic_sql(
        query="newest payment",
        allowed_columns={
            "payment": [
                "id",
                "invoice_id",
                "date",
                "amount",
                "receipt",
                "pay_mode_text",
                "created_at",
            ]
        },
        top_k=5,
    )

    assert built is not None
    sql, tables_used = built
    assert tables_used == ["payment"]
    assert "FROM payment" in sql
    assert "ORDER BY" in sql
    assert sql.endswith("LIMIT 1")
    assert "id" in sql
    assert "invoice_id" in sql


def test_deterministic_recent_payments_honors_explicit_limit():
    built = sql_generator.build_deterministic_sql(
        query="show me the 5 most recent payments",
        allowed_columns={
            "payment": [
                "id",
                "invoice_id",
                "date",
                "amount",
                "receipt",
                "pay_mode_text",
                "created_at",
            ]
        },
        top_k=3,
    )

    assert built is not None
    sql, _ = built
    assert sql.endswith("LIMIT 5")


def test_deterministic_recent_inquiries_uses_inquiry_table():
    built = sql_generator.build_deterministic_sql(
        query="show me the 5 most recent inquiries",
        allowed_columns={
            "inquiry": [
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
        },
        top_k=3,
    )

    assert built is not None
    sql, tables_used = built
    assert tables_used == ["inquiry"]
    assert "FROM inquiry" in sql
    assert "ORDER BY" in sql
    assert sql.endswith("LIMIT 5")
    assert "id" in sql
    assert "contact_id" in sql
    assert "created_at" in sql


def test_deterministic_recent_followups_uses_followup_table():
    built = sql_generator.build_deterministic_sql(
        query="show me the 5 most recent followups",
        allowed_columns={
            "followup": [
                "id",
                "inquiry_id",
                "employee_id",
                "notes",
                "followup_date",
                "outcome",
                "created_at",
            ]
        },
        top_k=3,
    )

    assert built is not None
    sql, tables_used = built
    assert tables_used == ["followup"]
    assert "FROM followup" in sql
    assert "ORDER BY" in sql
    assert sql.endswith("LIMIT 5")
    assert "id" in sql
    assert "inquiry_id" in sql
    assert "followup_date" in sql


def test_deterministic_recent_invoices_uses_invoice_table():
    built = sql_generator.build_deterministic_sql(
        query="show me the 5 most recent invoices",
        allowed_columns={
            "invoice": [
                "id",
                "member_id",
                "total_amount",
                "status",
                "issued_date",
                "due_date",
                "created_at",
            ]
        },
        top_k=3,
    )

    assert built is not None
    sql, tables_used = built
    assert tables_used == ["invoice"]
    assert "FROM invoice" in sql
    assert "ORDER BY" in sql
    assert sql.endswith("LIMIT 5")
    assert "id" in sql
    assert "member_id" in sql
    assert "created_at" in sql


def test_deterministic_recent_list_uses_explicit_table_name_generically():
    built = sql_generator.build_deterministic_sql(
        query="show me the 7 latest customer orders",
        allowed_columns={
            "customer_order": [
                "id",
                "customer_id",
                "order_number",
                "status",
                "created_at",
            ],
            "customer": ["id", "name", "created_at"],
        },
        top_k=5,
    )

    assert built is not None
    sql, tables_used = built
    assert tables_used == ["customer_order"]
    assert "FROM customer_order" in sql
    assert "ORDER BY created_at DESC, id DESC" in sql
    assert sql.endswith("LIMIT 7")


@pytest.mark.asyncio
async def test_generate_sql_recent_inquiries_uses_deterministic_path(
    client,
    mock_embed,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import react_agent

    monkeypatch.setattr(
        sql_generator,
        "load_columns_for_tables",
        AsyncMock(
            return_value={
                "inquiry": [
                    "id",
                    "contact_id",
                    "type",
                    "source",
                    "heard_from",
                    "created_at",
                ],
            }
        ),
    )
    monkeypatch.setattr(sql_generator, "run_explain", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        sql_generator.retrieve,
        "retrieve_groups",
        AsyncMock(
            return_value={
                "matched_groups": ["generic"],
                "tables_in_scope": ["inquiry"],
                "context": "Group: generic\nTables: inquiry",
                "results": [],
            }
        ),
    )
    react_run = AsyncMock(side_effect=AssertionError("ReAct should not run"))
    monkeypatch.setattr(react_agent, "run", react_run)

    response = await client.post(
        "/generate-sql",
        json={"query": "show me the 5 most recent inquiries"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert "FROM inquiry" in body["sql"]
    assert body["sql"].endswith("LIMIT 5")
    assert body["tables_used"] == ["inquiry"]
    assert body["matched_groups"] == ["deterministic_inquiry"]
    assert body["attempt_count"] == 0
    assert react_run.await_count == 0
    assert mock_embed.await_count == 0


@pytest.mark.asyncio
async def test_generate_sql_recent_followups_uses_deterministic_path(
    client,
    mock_embed,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import react_agent

    monkeypatch.setattr(
        sql_generator,
        "load_columns_for_tables",
        AsyncMock(
            return_value={
                "followup": [
                    "id",
                    "inquiry_id",
                    "employee_id",
                    "notes",
                    "followup_date",
                    "outcome",
                    "created_at",
                ],
            }
        ),
    )
    monkeypatch.setattr(sql_generator, "run_explain", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        sql_generator.retrieve,
        "retrieve_groups",
        AsyncMock(
            return_value={
                "matched_groups": ["generic"],
                "tables_in_scope": ["followup"],
                "context": "Group: generic\nTables: followup",
                "results": [],
            }
        ),
    )
    react_run = AsyncMock(side_effect=AssertionError("ReAct should not run"))
    monkeypatch.setattr(react_agent, "run", react_run)

    response = await client.post(
        "/generate-sql",
        json={"query": "show me the 5 most recent followups"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert "FROM followup" in body["sql"]
    assert body["sql"].endswith("LIMIT 5")
    assert body["tables_used"] == ["followup"]
    assert body["matched_groups"] == ["deterministic_followup"]
    assert body["attempt_count"] == 0
    assert body["review_prompt"]["question"].startswith("Does this SQL correctly answer")
    assert body["review_prompt"]["needs_review"] is False
    assert body["review_prompt"]["teach_payload"]["instruction_type"] == "correction"
    assert react_run.await_count == 0
    assert mock_embed.await_count == 0


@pytest.mark.asyncio
async def test_generate_sql_recent_invoices_uses_deterministic_path(
    client,
    mock_embed,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import react_agent

    monkeypatch.setattr(
        sql_generator,
        "load_columns_for_tables",
        AsyncMock(
            return_value={
                "invoice": [
                    "id",
                    "member_id",
                    "total_amount",
                    "status",
                    "issued_date",
                    "due_date",
                    "created_at",
                ],
            }
        ),
    )
    monkeypatch.setattr(sql_generator, "run_explain", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        sql_generator.retrieve,
        "retrieve_groups",
        AsyncMock(
            return_value={
                "matched_groups": ["generic"],
                "tables_in_scope": ["invoice"],
                "context": "Group: generic\nTables: invoice",
                "results": [],
            }
        ),
    )
    react_run = AsyncMock(side_effect=AssertionError("ReAct should not run"))
    monkeypatch.setattr(react_agent, "run", react_run)

    response = await client.post(
        "/generate-sql",
        json={"query": "show me the 5 most recent invoices"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert "FROM invoice" in body["sql"]
    assert body["sql"].endswith("LIMIT 5")
    assert body["tables_used"] == ["invoice"]
    assert body["matched_groups"] == ["deterministic_invoice"]
    assert body["attempt_count"] == 0
    assert body["review_prompt"]["needs_review"] is False
    assert react_run.await_count == 0
    assert mock_embed.await_count == 0


def test_deterministic_payment_sql_ignores_non_recent_payment_query():
    built = sql_generator.build_deterministic_sql(
        query="payments by branch",
        allowed_columns={"payment": ["id", "date", "amount"]},
        top_k=5,
    )

    assert built is None


def test_review_prompt_flags_followup_query_using_inquiry_table():
    from nl2sql_service import main

    prompt = main._build_sql_review_prompt(
        query="show me the 5 most recent followups",
        sql="SELECT id FROM inquiry ORDER BY created_at DESC LIMIT 5",
        tables_used=["inquiry"],
    )

    assert prompt.needs_review is True
    assert "not explicitly mentioned" in (prompt.reason or "")
    assert prompt.teach_payload["tables_affected"] == ["inquiry"]
    assert "intended table(s)" in prompt.teach_payload["content"]


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
    assert body["react_trace"]["total_iterations"] >= 4


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
    assert trace["total_iterations"] >= 2
    assert trace["final_action"] == "VALIDATE_AND_RETURN"

    actions = [step["action"] for step in trace["steps"]]
    assert actions[-2:] == [
        "GENERATE_SQL",
        "GENERATE_SQL",
    ]
    assert "Auto-validation: FAILED:" in trace["steps"][-2]["observation"]
    assert "Auto-validation: FAILED:" in trace["steps"][-1]["observation"]

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
async def test_generate_sql_service_budget_timeout_returns_rejected(
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import react_agent
    from nl2sql_service.config import settings

    async def slow_react_run(**kwargs):
        del kwargs
        await asyncio.sleep(0.05)

    monkeypatch.setattr(settings, "sql_generation_timeout", 0.01)
    monkeypatch.setattr(react_agent, "run", slow_react_run)

    response = await client.post(
        "/generate-sql",
        json={"query": "show unpaid invoices"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "rejected"
    assert any(
        warning["code"] == WarningCode.REQUEST_TIMEOUT.value
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
    assert {
        "status",
        "question",
        "suggestions",
        "original_query",
        "failure_reason",
        "cache_hit",
        "react_trace",
    }.issubset(body)
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
