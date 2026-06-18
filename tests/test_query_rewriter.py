from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from nl2sql_service import query_rewriter, react_agent
from nl2sql_service.config import settings
from nl2sql_service.llm.interfaces import LLMResponse
from nl2sql_service.models import GenerateSqlClarification


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


class _QueryConn:
    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        del sql, args
        return [
            {
                "content": "Employee billing context",
                "similarity": 0.91,
                "source": "employee_doc",
                "chunk_index": 0,
                "token_count": 10,
                "embedding_model": "bge-large-en-v1.5",
                "metadata": {},
            }
        ]


class _GroupConn:
    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        del sql, args
        return [
            {
                "content": "Group: employee access",
                "similarity": 0.93,
                "source": "employee_access_branch",
                "chunk_index": 0,
                "token_count": 12,
                "embedding_model": "bge-large-en-v1.5",
                "metadata": {
                    "type": "schema_group",
                    "tables": ["employee"],
                    "related_tables": ["contact"],
                    "group_description": "Employee access",
                },
            }
        ]


def test_parse_rewrite_response_valid_json() -> None:
    result = query_rewriter.parse_rewrite_response(
        '{"search_query":"show unpaid invoices by counselor employee"}',
        original_query="show unpaid invoices by counselor",
    )

    assert result == "show unpaid invoices by counselor employee"


def test_parse_rewrite_response_fenced_json() -> None:
    result = query_rewriter.parse_rewrite_response(
        '```json\n{"rewritten_query":"show counselor employee contact"}\n```',
        original_query="show counselor contact",
    )

    assert result == "show counselor employee contact"


def test_parse_rewrite_response_rejects_malformed_empty_and_overlong() -> None:
    assert query_rewriter.parse_rewrite_response("not-json", "show counselor") is None
    assert query_rewriter.parse_rewrite_response('{"search_query":""}', "show counselor") is None
    assert (
        query_rewriter.parse_rewrite_response(
            '{"search_query":"' + ("employee " * 100) + '"}',
            "show counselor",
            max_chars=80,
        )
        is None
    )


def test_rewrite_prompt_contains_literal_preservation_and_hints() -> None:
    hints = query_rewriter.parse_static_hints(
        "counselor,counsellor,counsellors -> employee"
    )
    prompt = query_rewriter.build_rewrite_prompt(
        "show unpaid invoices by counselor for Aman",
        hints,
    )

    assert "Preserve all names" in prompt
    assert "Aman" in prompt
    assert "counselor -> employee" in prompt
    assert "counsellor -> employee" in prompt


@pytest.mark.asyncio
async def test_query_endpoint_embeds_rewritten_search_text(
    app,
    client,
    mock_embed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import main

    app.state.pool = _FakePool(_QueryConn())
    monkeypatch.setattr(
        main.query_rewriter,
        "rewrite_search_query",
        AsyncMock(return_value="show unpaid invoices by counselor employee"),
    )

    response = await client.post(
        "/query",
        json={"query": "show unpaid invoices by counselor", "top_k": 3},
    )

    assert response.status_code == 200
    assert mock_embed.await_args.args[0] == ["show unpaid invoices by counselor employee"]


@pytest.mark.asyncio
async def test_query_groups_endpoint_embeds_rewritten_search_text(
    app,
    client,
    mock_embed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import instruction_store, main, pattern_store

    app.state.pool = _FakePool(_GroupConn())
    monkeypatch.setattr(
        main.query_rewriter,
        "rewrite_search_query",
        AsyncMock(return_value="show counselor contact employee"),
    )
    monkeypatch.setattr(pattern_store, "get_relevant_patterns", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        instruction_store,
        "get_relevant_instructions",
        AsyncMock(return_value=[]),
    )

    response = await client.post(
        "/query/groups",
        json={"query": "show counselor contact", "top_k": 3},
    )

    assert response.status_code == 200
    assert mock_embed.await_args.args[0] == ["show counselor contact employee"]


@pytest.mark.asyncio
async def test_rewrite_timeout_falls_back_to_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _EmptyClient:
        async def generate(self, **kwargs):
            assert kwargs["response_format"] == "json"
            return LLMResponse(text="")

    monkeypatch.setattr(settings, "query_rewrite_enabled", True)
    monkeypatch.setattr(
        query_rewriter,
        "get_model_client",
        lambda **kwargs: _EmptyClient(),
    )
    monkeypatch.setattr(
        query_rewriter,
        "build_rewrite_hints",
        AsyncMock(return_value=["counselor -> employee"]),
    )

    result = await query_rewriter.rewrite_search_query(
        "show counselor contact today",
        pool=object(),
        settings=settings,
    )

    assert result.startswith("show counselor contact today")
    assert "email" in result
    assert "phone" in result


@pytest.mark.asyncio
async def test_rewrite_skips_short_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    build_hints = AsyncMock(return_value=["counselor -> employee"])
    call_model = AsyncMock(return_value="should not be used")
    monkeypatch.setattr(settings, "query_rewrite_enabled", True)
    monkeypatch.setattr(query_rewriter, "build_rewrite_hints", build_hints)
    monkeypatch.setattr(query_rewriter, "_call_rewrite_model", call_model)

    result = await query_rewriter.rewrite_search_query(
        "newest payment",
        pool=object(),
        settings=settings,
    )

    assert result == "newest payment"
    assert build_hints.await_count == 0
    assert call_model.await_count == 0


@pytest.mark.asyncio
async def test_rewrite_expands_synonym_hints_from_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "query_rewrite_enabled", True)
    monkeypatch.setattr(
        query_rewriter,
        "build_rewrite_hints",
        AsyncMock(return_value=["counselor -> employee"]),
    )
    monkeypatch.setattr(
        query_rewriter,
        "_call_rewrite_model",
        AsyncMock(return_value="find contact name for aman"),
    )

    result = await query_rewriter.rewrite_search_query(
        "find contact name for aman",
        pool=object(),
        settings=settings,
    )

    assert "first name" in result
    assert "last name" in result
    assert "email" in result
    assert "phone" in result


@pytest.mark.asyncio
async def test_react_agent_rewrites_once_and_reuses_expansion(
    mock_embed,
    mock_ollama,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rewrite_mock = AsyncMock(return_value="show unpaid invoices by counselor employee")
    retrieve_mock = AsyncMock(
        return_value={
            "matched_groups": ["billing"],
            "tables_in_scope": ["invoice", "employee"],
            "context": "Group: billing",
            "results": [],
        }
    )
    monkeypatch.setattr(settings, "query_rewrite_enabled", True)
    monkeypatch.setattr(settings, "react_confidence_threshold", 2.0)
    print("REACT CONFIDENCE THRESHOLD IS:", settings.react_confidence_threshold)
    monkeypatch.setattr(react_agent.query_rewriter, "rewrite_search_query", rewrite_mock)
    monkeypatch.setattr(react_agent, "retrieve_groups", retrieve_mock)
    monkeypatch.setattr(react_agent, "retrieve_past_corrections", AsyncMock(return_value=[]))
    class MockColumnDoc:
        def __init__(self, table: str, col: str):
            self.metadata = {"table_name": table, "column_name": col}
    monkeypatch.setattr(
        react_agent,
        "retrieve_column_catalog",
        AsyncMock(return_value=[MockColumnDoc("invoice", "id"), MockColumnDoc("employee", "id")]),
    )
    monkeypatch.setattr(react_agent, "retrieve_join_paths", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        react_agent,
        "call_reasoning_model",
        AsyncMock(
            side_effect=[
                (
                    "Need better billing context",
                    "ACTION: RETRIEVE_MORE_CONTEXT\nINPUT: billing unpaid invoices",
                    [],
                ),
                (
                    "Still ambiguous",
                    "ACTION: ASK_CLARIFICATION\nINPUT: need invoice status",
                    [],
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        react_agent,
        "build_clarification",
        AsyncMock(
            return_value=GenerateSqlClarification(
                question="Which status?",
                suggestions=["show unpaid invoices by employee", "show paid invoices"],
                original_query="show unpaid invoices by counselor",
                failure_reason="need invoice status",
                react_trace=None,
            )
        ),
    )

    response = await react_agent.run(
        query="show unpaid invoices by counselor",
        pool=object(),
        settings=settings,
        top_k=4,
    )
    print("RESPONSE:", response)

    assert response.status == "clarification_needed"
    assert rewrite_mock.await_count == 1
    assert retrieve_mock.await_count == 1
    assert (
        retrieve_mock.await_args_list[0].kwargs["search_query"]
        == "show unpaid invoices by counselor employee"
    )
