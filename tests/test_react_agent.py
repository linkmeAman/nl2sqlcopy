from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nl2sql_service import react_agent
from nl2sql_service.config import settings
from nl2sql_service.llm.interfaces import LLMResponse
from nl2sql_service.models import ReActAction, SqlWarning, WarningCode


_COLUMNS = {
    "invoice": ["id", "member_id", "amount", "status"],
    "member": ["id", "name", "status"],
    "payment": ["id", "invoice_id", "amount", "method", "created_at"],
}


@pytest.fixture(autouse=True)
def mock_schema_and_explain(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load_columns(tables: list[str], settings) -> dict[str, list[str]]:
        del settings
        return {table: _COLUMNS[table] for table in tables if table in _COLUMNS}

    monkeypatch.setattr(settings, "react_max_iterations", 4)
    monkeypatch.setattr(react_agent, "load_columns_for_tables", AsyncMock(side_effect=load_columns))
    monkeypatch.setattr(react_agent, "run_explain", AsyncMock(return_value=[]))


@pytest.mark.asyncio
async def test_happy_path_generate_then_validate(
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_react_call_ollama,
):
    del mock_react_retrieve, mock_react_call_ollama
    mock_call_reasoning_model.side_effect = [
        (
            "I should generate SQL for billing tables",
            "ACTION: GENERATE_SQL\nINPUT: generate select",
            [],
        ),
        (
            "The generated SQL should now be validated",
            "ACTION: VALIDATE_AND_RETURN\nINPUT: validate current SQL",
            [],
        ),
    ]

    response = await react_agent.run(
        query="show unpaid invoices",
        pool=object(),
        settings=settings,
    )

    assert response.status == "ok"
    assert response.react_trace is not None
    assert response.react_trace.total_iterations == 1
    assert len(response.react_trace.steps) == 1
    assert response.react_trace.steps[0].action == ReActAction.GENERATE_SQL
    assert response.react_trace.final_action == ReActAction.VALIDATE_AND_RETURN
    assert "Auto-validation: PASSED" in response.react_trace.steps[0].observation
    assert response.react_trace.steps[0].thought


@pytest.mark.asyncio
async def test_wrong_table_retrieve_then_generate_and_validate(
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_react_call_ollama,
):
    del mock_react_call_ollama
    mock_call_reasoning_model.side_effect = [
        (
            "The table context looks wrong, so I should retrieve better context",
            "ACTION: RETRIEVE_MORE_CONTEXT\nINPUT: billing unpaid invoices",
            [],
        ),
        (
            "The updated billing context is enough to generate SQL",
            "ACTION: GENERATE_SQL\nINPUT: generate select",
            [],
        ),
        (
            "The SQL can now be validated",
            "ACTION: VALIDATE_AND_RETURN\nINPUT: validate current SQL",
            [],
        ),
    ]

    response = await react_agent.run(
        query="show unpaid invoices",
        pool=object(),
        settings=settings,
        top_k=7,
    )

    assert response.status == "ok"
    assert response.react_trace is not None
    assert response.react_trace.total_iterations == 2
    assert response.react_trace.steps[0].action == ReActAction.RETRIEVE_MORE_CONTEXT
    assert response.react_trace.steps[1].action == ReActAction.GENERATE_SQL
    assert response.react_trace.final_action == ReActAction.VALIDATE_AND_RETURN
    assert "tables_in_scope" in response.react_trace.steps[0].observation
    assert "Columns refreshed" in response.react_trace.steps[0].observation
    assert mock_react_retrieve.await_args_list[0].kwargs["top_k"] == 7
    assert mock_react_retrieve.await_args_list[1].kwargs["top_k"] == 7


@pytest.mark.asyncio
async def test_wrong_column_fetch_schema_then_retry(
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_react_call_ollama,
):
    del mock_react_retrieve, mock_react_call_ollama
    mock_call_reasoning_model.side_effect = [
        (
            "The payment columns need to be checked before generating SQL",
            "ACTION: FETCH_SCHEMA\nINPUT: payment",
            [],
        ),
        (
            "Payment columns are known, so generate SQL",
            "ACTION: GENERATE_SQL\nINPUT: generate select",
            [],
        ),
        (
            "The SQL is ready for validation",
            "ACTION: VALIDATE_AND_RETURN\nINPUT: validate current SQL",
            [],
        ),
    ]

    response = await react_agent.run(
        query="show payments",
        pool=object(),
        settings=settings,
    )

    assert response.status == "ok"
    assert response.react_trace is not None
    assert response.react_trace.steps[0].action == ReActAction.FETCH_SCHEMA
    assert "payment" in response.react_trace.steps[0].observation


@pytest.mark.asyncio
async def test_simple_select_star_is_narrowed_before_validation(
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_react_call_ollama,
):
    del mock_react_retrieve
    mock_call_reasoning_model.side_effect = [
        (
            "Generate payment SQL",
            "ACTION: GENERATE_SQL\nINPUT: show recent payments",
            [],
        ),
        (
            "Validate payment SQL",
            "ACTION: VALIDATE_AND_RETURN\nINPUT: validate current SQL",
            [],
        ),
    ]
    mock_react_call_ollama.return_value = (
        "SELECT * FROM payment ORDER BY created_at DESC LIMIT 5;",
        [],
    )

    response = await react_agent.run(
        query="show me the 5 most recent payments",
        pool=object(),
        settings=settings,
    )

    assert response.status == "ok"
    assert response.sql.startswith("SELECT id")
    assert "*" not in response.sql
    assert "invoice_id" in response.sql
    assert "ORDER BY created_at DESC LIMIT 5" in response.sql


@pytest.mark.asyncio
async def test_final_iteration_generated_sql_is_auto_validated(
    monkeypatch: pytest.MonkeyPatch,
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_react_call_ollama,
):
    del mock_react_retrieve
    monkeypatch.setattr(settings, "react_max_iterations", 1)
    mock_call_reasoning_model.return_value = (
        "Generate SQL on the final allowed iteration",
        "ACTION: GENERATE_SQL\nINPUT: show recent payments",
        [],
    )
    mock_react_call_ollama.return_value = (
        "SELECT id, invoice_id, created_at, amount FROM payment "
        "ORDER BY created_at DESC LIMIT 5;",
        [],
    )

    response = await react_agent.run(
        query="show me the 5 most recent payments",
        pool=object(),
        settings=settings,
    )

    assert response.status == "ok"
    assert response.react_trace is not None
    assert response.react_trace.total_iterations == 1
    assert response.react_trace.final_action == ReActAction.VALIDATE_AND_RETURN
    assert response.react_trace.steps[-1].action == ReActAction.GENERATE_SQL
    assert "Auto-validation: PASSED" in response.react_trace.steps[-1].observation


@pytest.mark.asyncio
async def test_reasoning_model_chooses_give_up(
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_build_clarification,
):
    del mock_react_retrieve, mock_build_clarification
    mock_call_reasoning_model.return_value = (
        "The schema context is insufficient to continue",
        "ACTION: GIVE_UP\nINPUT: Cannot determine correct table structure",
        [],
    )

    response = await react_agent.run(
        query="ambiguous query",
        pool=object(),
        settings=settings,
    )

    assert response.status == "clarification_needed"
    assert response.failure_reason == "Cannot determine correct table structure"
    assert response.react_trace is not None
    assert response.react_trace.final_action == ReActAction.GIVE_UP


@pytest.mark.asyncio
async def test_unparseable_empty_planner_response_recovers_to_generate_then_validate(
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_react_call_ollama,
):
    del mock_react_retrieve, mock_react_call_ollama
    mock_call_reasoning_model.return_value = ("", "", [])

    response = await react_agent.run(
        query="show the 5 most recent payments",
        pool=object(),
        settings=settings,
    )

    assert response.status == "ok"
    assert response.react_trace is not None
    assert response.react_trace.total_iterations == 1
    assert response.react_trace.steps[0].action == ReActAction.GENERATE_SQL
    assert response.react_trace.final_action == ReActAction.VALIDATE_AND_RETURN
    assert "Auto-validation: PASSED" in response.react_trace.steps[0].observation


@pytest.mark.asyncio
async def test_explicit_unknown_action_returns_clarification(
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_build_clarification,
):
    del mock_react_retrieve, mock_build_clarification
    mock_call_reasoning_model.return_value = (
        "",
        "ACTION: NOT_REAL\nINPUT: bad input",
        [],
    )

    response = await react_agent.run(
        query="show payments",
        pool=object(),
        settings=settings,
    )

    assert response.status == "clarification_needed"
    assert response.react_trace is not None
    assert response.react_trace.final_action == ReActAction.GIVE_UP


@pytest.mark.asyncio
async def test_max_iterations_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_react_call_ollama,
    mock_build_clarification,
):
    del mock_react_retrieve, mock_build_clarification
    monkeypatch.setattr(settings, "react_max_iterations", 3)
    mock_call_reasoning_model.return_value = (
        "I will keep generating SQL",
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
    assert response.react_trace is not None
    assert response.react_trace.total_iterations == 3
    assert response.react_trace.steps[-1].action == ReActAction.GENERATE_SQL
    assert response.react_trace.final_action == ReActAction.VALIDATE_AND_RETURN
    assert WarningCode.MAX_RETRIES_EXCEEDED.value in response.failure_reason


@pytest.mark.asyncio
async def test_reasoning_model_timeout_rejected_http_200(
    client,
    mock_call_reasoning_model,
    mock_react_retrieve,
):
    del mock_react_retrieve
    mock_call_reasoning_model.return_value = (
        "",
        "",
        [
            SqlWarning(
                code=WarningCode.OLLAMA_TIMEOUT,
                message="Reasoning model timed out after 45s",
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
    assert not any(
        warning["code"] == WarningCode.MAX_RETRIES_EXCEEDED.value
        for warning in body["warnings"]
    )


@pytest.mark.asyncio
async def test_retry_generation_uses_refinement_prompt_and_planner_input(
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_react_call_ollama,
):
    del mock_react_retrieve
    mock_call_reasoning_model.side_effect = [
        (
            "Generate an initial query",
            "ACTION: GENERATE_SQL\nINPUT: draft a first attempt",
            [],
        ),
        (
            "The prior query used the wrong table, so correct it",
            "ACTION: GENERATE_SQL\nINPUT: fix the table name and keep unpaid status",
            [],
        ),
    ]
    mock_react_call_ollama.side_effect = [
        ("SELECT * FROM forbidden_table", []),
        ("SELECT id, amount FROM invoice WHERE status='unpaid'", []),
    ]

    response = await react_agent.run(
        query="show unpaid invoices",
        pool=object(),
        settings=settings,
    )

    assert response.status == "ok"
    assert mock_react_call_ollama.await_count == 2
    first_prompt = mock_react_call_ollama.await_args_list[0].kwargs["prompt"]
    second_prompt = mock_react_call_ollama.await_args_list[1].kwargs["prompt"]
    assert "Planner instruction: draft a first attempt" in first_prompt
    assert "choose concise, semantically relevant columns" in first_prompt
    assert "Use SELECT * only when the user explicitly asks" in first_prompt
    assert "Honor explicit row counts with LIMIT." in first_prompt
    assert "Previous SQL:" in second_prompt
    assert "forbidden_table" in second_prompt
    assert "TABLE_OUT_OF_SCOPE" in second_prompt
    assert "Planner instruction: fix the table name and keep unpaid status" in second_prompt
    assert "Correct every validation error listed above." in second_prompt
    assert "Do not reuse disallowed tables or columns from previous SQL." in second_prompt
    assert "choose concise, semantically relevant columns" in second_prompt


@pytest.mark.asyncio
async def test_react_trace_in_clarification_response(
    client,
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_build_clarification,
):
    del mock_react_retrieve, mock_build_clarification
    mock_call_reasoning_model.return_value = (
        "There is not enough information to continue",
        "ACTION: GIVE_UP\nINPUT: Cannot determine correct table structure",
        [],
    )

    response = await client.post(
        "/generate-sql",
        json={"query": "ambiguous query"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "clarification_needed"
    assert "react_trace" in body
    assert isinstance(body["react_trace"]["steps"], list)
    assert body["react_trace"]["total_iterations"] >= 1


@pytest.mark.asyncio
async def test_mysql_explain_unavailable_does_not_cause_rejection(
    monkeypatch: pytest.MonkeyPatch,
    mock_call_reasoning_model,
    mock_react_retrieve,
    mock_react_call_ollama,
):
    del mock_react_retrieve, mock_react_call_ollama
    mock_call_reasoning_model.side_effect = [
        (
            "Generate SQL before validation",
            "ACTION: GENERATE_SQL\nINPUT: generate select",
            [],
        ),
        (
            "Validate the generated SQL",
            "ACTION: VALIDATE_AND_RETURN\nINPUT: validate current SQL",
            [],
        ),
    ]
    monkeypatch.setattr(
        react_agent,
        "run_explain",
        AsyncMock(
            return_value=[
                SqlWarning(
                    code=WarningCode.MYSQL_EXPLAIN_UNAVAILABLE,
                    message="MySQL unavailable",
                )
            ]
        ),
    )

    response = await react_agent.run(
        query="show unpaid invoices",
        pool=object(),
        settings=settings,
    )

    assert response.status == "ok"
    assert any(
        warning.code == WarningCode.MYSQL_EXPLAIN_UNAVAILABLE
        for warning in response.warnings
    )
    assert response.attempt_count == 1


def test_extract_think_block_parsing():
    thought, answer = react_agent.extract_think_block(
        "<think>I need to check tables</think>\n"
        "ACTION: GENERATE_SQL\nINPUT: generate select"
    )

    assert thought == "I need to check tables"
    assert answer == "ACTION: GENERATE_SQL\nINPUT: generate select"

    thought, answer = react_agent.extract_think_block(
        "ACTION: GENERATE_SQL\nINPUT: generate select"
    )

    assert thought == ""
    assert answer == "ACTION: GENERATE_SQL\nINPUT: generate select"


@pytest.mark.asyncio
async def test_call_reasoning_model_uses_thinking_action_when_response_empty(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeClient:
        provider_name = "fake"

        async def generate(self, **kwargs):
            assert kwargs["enable_thinking"] is True
            assert kwargs["max_tokens"] == 800
            assert kwargs["response_format"] == "json"
            return LLMResponse(
                text="",
                thought='{"action":"GENERATE_SQL","input":"show payments"}',
            )

    monkeypatch.setattr(react_agent, "get_model_client", lambda **kwargs: FakeClient())

    thought, answer, warnings = await react_agent.call_reasoning_model(
        "prompt",
        settings,
    )

    assert warnings == []
    assert thought == ""
    assert answer == '{"action":"GENERATE_SQL","input":"show payments"}'


def test_parse_action_handles_all_valid_actions():
    for action in ReActAction:
        parsed_action, action_input = react_agent.parse_action(
            f"ACTION: {action.value}\nINPUT: useful input"
        )
        assert parsed_action == action
        assert action_input == "useful input"

    parsed_action, action_input = react_agent.parse_action(
        "ACTION: NOT_REAL\nINPUT: bad input"
    )
    assert parsed_action == ReActAction.GIVE_UP
    assert action_input == "Could not parse action"

    parsed_action, action_input = react_agent.parse_action("ACTION: GENERATE_SQL")
    assert parsed_action == ReActAction.GENERATE_SQL
    assert action_input == ""

    parsed_action, action_input = react_agent.parse_action(
        "ACTION: generate_sql\nINPUT: generate select"
    )
    assert parsed_action == ReActAction.GENERATE_SQL
    assert action_input == "generate select"


def test_parse_action_tolerates_format_drift_patterns():
    parsed_action, action_input = react_agent.parse_action(
        "**Action** - `generate sql`\n**Instruction**: include latest payments"
    )
    assert parsed_action == ReActAction.GENERATE_SQL
    assert action_input == "include latest payments"

    parsed_action, action_input = react_agent.parse_action(
        '{"action":"FETCH_SCHEMA","input":"payment, invoice"}'
    )
    assert parsed_action == ReActAction.FETCH_SCHEMA
    assert action_input == "payment, invoice"

    parsed_action, action_input = react_agent.parse_action(
        "Next step is validate and return because SQL now passes checks."
    )
    assert parsed_action == ReActAction.VALIDATE_AND_RETURN
    assert action_input == ""

    parsed_action, action_input = react_agent.parse_action(
        "ACTION_INPUT: tighten filters\nACTION: RETRIEVE_CONTEXT"
    )
    assert parsed_action == ReActAction.RETRIEVE_MORE_CONTEXT
    assert action_input == "tighten filters"

    parsed_action, action_input = react_agent.parse_action(
        "I should generate a SQL query from the retrieved payment context."
    )
    assert parsed_action == ReActAction.GENERATE_SQL
    assert action_input == ""
