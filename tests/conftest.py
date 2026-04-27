from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from nl2sql_service import embed
from nl2sql_service.config import settings
from nl2sql_service.models import SqlWarning


@pytest.fixture
def app():
    from nl2sql_service.main import app as fastapi_app

    fastapi_app.state.pool = object()
    settings.llm_max_retries = 2
    settings.react_max_iterations = 4
    settings.top_k = 5
    return fastapi_app


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as async_client:
        yield async_client


@pytest.fixture
def mock_embed(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    async_mock = AsyncMock(return_value=[[0.1] * 1024])
    monkeypatch.setattr(embed, "embed_texts", async_mock)
    return async_mock


@pytest.fixture
def mock_ollama(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import react_agent
    from nl2sql_service import sql_generator

    async_mock: AsyncMock = AsyncMock(
        return_value=(
            "SELECT id, amount FROM invoice WHERE status='unpaid'",
            [],
        )
    )
    reasoning_mock: AsyncMock = AsyncMock(
        side_effect=[
            (
                "I should generate SQL for billing tables",
                "ACTION: GENERATE_SQL\nINPUT: generate select",
                [],
            ),
            (
                "I should validate the generated SQL",
                "ACTION: VALIDATE_AND_RETURN\nINPUT: validate current SQL",
                [],
            ),
            (
                "The prior SQL failed, so I should generate a corrected query",
                "ACTION: GENERATE_SQL\nINPUT: generate corrected select",
                [],
            ),
            (
                "I should validate the corrected SQL",
                "ACTION: VALIDATE_AND_RETURN\nINPUT: validate current SQL",
                [],
            ),
        ]
    )
    load_columns_mock: AsyncMock = AsyncMock(
        return_value={
            "invoice": ["id", "amount", "total", "member_id", "status"],
            "member": ["id", "name", "status"],
            "payment": ["id", "invoice_id", "amount", "method"],
        }
    )
    monkeypatch.setattr(sql_generator, "call_ollama", async_mock)
    monkeypatch.setattr(react_agent, "call_ollama", async_mock)
    monkeypatch.setattr(react_agent, "call_reasoning_model", reasoning_mock)
    monkeypatch.setattr(react_agent, "load_columns_for_tables", load_columns_mock)
    monkeypatch.setattr(react_agent, "run_explain", AsyncMock(return_value=[]))
    return async_mock


@pytest.fixture
def mock_retrieve_groups(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import react_agent
    from nl2sql_service import retrieve

    async_mock: AsyncMock = AsyncMock(
        return_value={
            "matched_groups": ["billing"],
            "tables_in_scope": ["invoice", "member", "payment"],
            "context": "Group: billing\nRoot: member\n...",
            "results": [],
        }
    )
    monkeypatch.setattr(retrieve, "retrieve_groups", async_mock)
    monkeypatch.setattr(react_agent, "retrieve_groups", async_mock)
    return async_mock


@pytest.fixture
def mock_call_reasoning_model(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import react_agent

    async_mock: AsyncMock = AsyncMock(
        return_value=(
            "I should generate SQL for billing tables",
            "ACTION: GENERATE_SQL\nINPUT: generate select",
            [],
        )
    )
    monkeypatch.setattr(react_agent, "call_reasoning_model", async_mock)
    return async_mock


@pytest.fixture
def mock_react_retrieve(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import react_agent

    async_mock: AsyncMock = AsyncMock(
        return_value={
            "matched_groups": ["billing"],
            "tables_in_scope": ["invoice", "member", "payment"],
            "context": "Group: billing\nRoot: member\n...",
            "results": [],
        }
    )
    monkeypatch.setattr(react_agent, "retrieve_groups", async_mock)
    return async_mock


@pytest.fixture
def mock_react_call_ollama(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import react_agent

    async_mock: AsyncMock = AsyncMock(
        return_value=(
            "SELECT id, amount FROM invoice WHERE status='unpaid'",
            [],
        )
    )
    monkeypatch.setattr(react_agent, "call_ollama", async_mock)
    return async_mock


@pytest.fixture
def tmp_rag_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "rag_schema"
    entities = root / "entities"
    relations = root / "relations"
    graph = root / "graph"
    rules = root / "rules"

    for directory in (entities, relations, graph, rules):
        directory.mkdir(parents=True, exist_ok=True)

    entity = {
        "entity_id": "billing",
        "chunk_group_name": "billing",
        "root_table": "member",
        "root_table_ref": "member",
        "included_tables": ["invoice", "payment"],
        "summarized_tables": [],
        "referenced_tables": [],
        "excluded_tables": [],
        "chunking_rules": [],
        "rationale": "Billing test entity",
        "secondary_memberships": [],
        "table_ref_map": {},
    }
    (entities / "entity__billing.json").write_text(
        json.dumps(entity),
        encoding="utf-8",
    )
    (graph / "table_classification.json").write_text("{}", encoding="utf-8")
    (graph / "table_graph.json").write_text('{"nodes": []}', encoding="utf-8")
    (graph / "view_registry.json").write_text("[]", encoding="utf-8")
    (rules / "chunking_rules.json").write_text("{}", encoding="utf-8")
    (rules / "onboarding_rules.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("RAG_SCHEMA_DIR", str(root))
    return root


@pytest.fixture
def mock_load_columns_for_ingest(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import chunker

    async_mock: AsyncMock = AsyncMock(
        return_value={
            "invoice": [
                "id",
                "member_id",
                "total_amount",
                "status",
                "issued_date",
                "due_date",
            ],
            "payment": [
                "id",
                "invoice_id",
                "amount",
                "date",
                "method",
                "reference",
            ],
            "member": [
                "id",
                "contact_id",
                "plan",
                "start_date",
                "end_date",
                "status",
            ],
            "inquiry": [
                "id",
                "contact_id",
                "assigned_employee_id",
                "subject",
                "status",
                "created_at",
            ],
            "followup": [
                "id",
                "inquiry_id",
                "employee_id",
                "notes",
                "followup_date",
                "outcome",
                "created_at",
            ],
            "employee": [
                "id",
                "name",
                "email",
                "role",
                "department",
                "is_active",
            ],
        }
    )
    monkeypatch.setattr(chunker, "load_columns_for_tables", async_mock)
    return async_mock


@pytest.fixture
def mock_load_columns_unavailable(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import chunker

    async_mock: AsyncMock = AsyncMock(return_value={})
    monkeypatch.setattr(chunker, "load_columns_for_tables", async_mock)
    return async_mock


@pytest.fixture
def mock_ask_execute_sql(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import mysql_executor

    async_mock: AsyncMock = AsyncMock(
        return_value=(
            ["id", "amount"],
            [(1, 100), (2, 200)],
            [],
        )
    )
    monkeypatch.setattr(mysql_executor, "execute_sql", async_mock)
    return async_mock


@pytest.fixture
def mock_ask_answer_generator(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import answer_generator

    async_mock: AsyncMock = AsyncMock(
        return_value=(
            "There are 2 matching rows.",
            [],
        )
    )
    monkeypatch.setattr(answer_generator, "generate_answer", async_mock)
    return async_mock


@pytest.fixture
def mock_entity_with_enrichment() -> dict:
    return {
        "entity_id": "billing",
        "chunk_group_name": "Billing",
        "root_table": "invoice",
        "root_table_ref": "invoice",
        "included_tables": ["payment", "member"],
        "summarized_tables": [],
        "referenced_tables": ["employee"],
        "excluded_tables": [],
        "relation_ids": ["payment.invoice_id->invoice.id"],
        "chunking_rules": ["max_tokens:400"],
        "rationale": "Billing and payment tracking",
        "table_ref_map": {},
        "secondary_memberships": [],
        "business_aliases": {
            "employee": ["counselor", "staff"],
            "invoice": ["bill", "fee"],
            "member": ["client"],
        },
        "example_questions": [
            "show unpaid invoices by counselor",
            "total revenue this month",
            "list overdue members",
        ],
    }


@pytest.fixture
def mock_entity_no_enrichment() -> dict:
    return {
        "entity_id": "billing",
        "chunk_group_name": "Billing",
        "root_table": "invoice",
        "root_table_ref": "invoice",
        "included_tables": ["payment", "member"],
        "summarized_tables": [],
        "referenced_tables": ["employee"],
        "excluded_tables": [],
        "relation_ids": ["payment.invoice_id->invoice.id"],
        "chunking_rules": ["max_tokens:400"],
        "rationale": "Billing and payment tracking",
        "table_ref_map": {},
        "secondary_memberships": [],
    }
