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
from nl2sql_service.models import (
    GenerateSqlClarification,
    LearningStatus,
    SqlWarning,
    TeachResponse,
)


class _NoopTransaction:
    async def __aenter__(self) -> "_NoopTransaction":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _NoopConn:
    def __init__(self) -> None:
        self.cache_epoch = 1

    def transaction(self) -> _NoopTransaction:
        return _NoopTransaction()

    async def fetch(self, sql: str, *args):
        del sql, args
        return []

    async def fetchrow(self, sql: str, *args):
        del args
        if "SELECT cache_epoch" in sql:
            return {"cache_epoch": self.cache_epoch}
        if "COUNT(*)::bigint AS db_query_cache_size" in sql:
            return {"db_query_cache_size": 0, "cache_epoch": self.cache_epoch}
        if "pending_active_count" in sql and "pending_expired_count" in sql:
            return {
                "pending_active_count": 0,
                "pending_expired_count": 0,
                "oldest_pending_created_at": None,
                "next_pending_expiry_at": None,
            }
        if "UPDATE nl2sql_cache_state" in sql:
            self.cache_epoch += 1
            return {"cache_epoch": self.cache_epoch}
        return None

    async def execute(self, sql: str, *args) -> str:
        del args
        if "DELETE FROM nl2sql_query_cache" in sql:
            return "DELETE 0"
        if "INSERT INTO nl2sql_cache_state" in sql:
            return "INSERT 0 1"
        return "OK"


class _NoopAcquire:
    def __init__(self, conn: _NoopConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _NoopConn:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _NoopPool:
    def __init__(self) -> None:
        self.conn = _NoopConn()

    def acquire(self) -> _NoopAcquire:
        return _NoopAcquire(self.conn)


@pytest.fixture(autouse=True)
def disable_query_rewrite_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "query_rewrite_enabled", False)


@pytest.fixture(autouse=True)
def disable_governance_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from nl2sql_service import rulebook

    monkeypatch.setattr(settings, "governance_enabled", False)
    monkeypatch.setattr(settings, "governance_enabled_rules", "all")
    monkeypatch.setattr(settings, "governance_inject_react", True)
    monkeypatch.setattr(settings, "governance_inject_sql", True)
    monkeypatch.setattr(settings, "governance_inject_answer", True)
    monkeypatch.setattr(rulebook, "_config", None)


@pytest.fixture(autouse=True)
def clear_in_memory_caches() -> None:
    from nl2sql_service.cache import ask_cache, embed_cache, semantic_sql_cache, sql_cache

    embed_cache.clear()
    sql_cache.invalidate_all()
    semantic_sql_cache.invalidate_all()
    ask_cache.invalidate_all()


@pytest.fixture
def app():
    from nl2sql_service.main import app as fastapi_app

    fastapi_app.state.pool = _NoopPool()
    settings.llm_max_retries = 2
    settings.react_max_iterations = 4
    settings.sql_generation_timeout = 90
    settings.ask_timeout = 105
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
def mock_pattern_store_empty(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import pattern_store

    async_mock: AsyncMock = AsyncMock(return_value=[])
    monkeypatch.setattr(pattern_store, "get_relevant_patterns", async_mock)
    return async_mock


@pytest.fixture
def mock_pattern_store_with_join_pattern(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import pattern_store

    async_mock: AsyncMock = AsyncMock(
        return_value=[
            {
                "id": 1,
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
                "use_count": 5,
                "last_used_at": "2026-04-27T10:00:00",
            }
        ]
    )
    monkeypatch.setattr(pattern_store, "get_relevant_patterns", async_mock)
    return async_mock


@pytest.fixture
def mock_instruction_store_empty(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import instruction_store
    from nl2sql_service import retrieve

    async_mock: AsyncMock = AsyncMock(return_value=[])
    monkeypatch.setattr(instruction_store, "get_relevant_instructions", async_mock)
    monkeypatch.setattr(retrieve.instruction_store, "get_relevant_instructions", async_mock)
    return async_mock


@pytest.fixture
def mock_instruction_store_with_rules(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import instruction_store
    from nl2sql_service import retrieve

    async_mock: AsyncMock = AsyncMock(
        return_value=[
            {
                "id": 1,
                "instruction_type": "table_relationship",
                "content": "employee.contact_id = contact.id",
                "embedding_source": (
                    "Table relationship: employee.contact_id = contact.id\n"
                    "Tables: employee, contact"
                ),
                "tables_affected": ["employee", "contact"],
                "confidence_score": 1.0,
                "is_verified": True,
                "is_active": True,
                "use_count": 8,
                "success_count": 7,
                "failure_count": 1,
                "last_used_at": "2026-04-28T10:00:00",
            },
            {
                "id": 2,
                "instruction_type": "term_mapping",
                "content": "counselor means employee table",
                "embedding_source": (
                    "Term mapping: counselor means employee table\n"
                    "Related tables: employee"
                ),
                "tables_affected": ["employee"],
                "confidence_score": 0.9,
                "is_verified": True,
                "is_active": True,
                "use_count": 5,
                "success_count": 5,
                "failure_count": 0,
                "last_used_at": "2026-04-28T09:00:00",
            },
        ]
    )
    monkeypatch.setattr(instruction_store, "get_relevant_instructions", async_mock)
    monkeypatch.setattr(retrieve.instruction_store, "get_relevant_instructions", async_mock)
    return async_mock


@pytest.fixture
def mock_process_teach_request(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import instruction_store
    from nl2sql_service import main

    async_mock: AsyncMock = AsyncMock(
        return_value=TeachResponse(
            learning_status=LearningStatus.SAVED_NEW,
            message="This instruction is new. I've saved it.",
            instruction_id=42,
        )
    )
    monkeypatch.setattr(instruction_store, "process_teach_request", async_mock)
    monkeypatch.setattr(main, "process_teach_request", async_mock)
    return async_mock


@pytest.fixture
def mock_detect_conflict_none(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import instruction_store

    async_mock: AsyncMock = AsyncMock(return_value=None)
    monkeypatch.setattr(instruction_store, "detect_conflict", async_mock)
    return async_mock


@pytest.fixture
def mock_detect_conflict_found(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import instruction_store

    async_mock: AsyncMock = AsyncMock(
        return_value={
            "id": 10,
            "instruction_type": "table_relationship",
            "content": "employee links to contact via employee_id",
            "confidence_score": 0.7,
            "is_verified": False,
            "use_count": 2,
        }
    )
    monkeypatch.setattr(instruction_store, "detect_conflict", async_mock)
    return async_mock


@pytest.fixture
def mock_save_pattern(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import pattern_store

    async_mock: AsyncMock = AsyncMock(return_value=None)
    monkeypatch.setattr(pattern_store, "save_pattern", async_mock)
    return async_mock


@pytest.fixture
def mock_build_clarification(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    from nl2sql_service import react_agent

    async def build_mock(**kwargs):
        return GenerateSqlClarification(
            question="Are you searching for an employee or a contact?",
            suggestions=[
                "find employee with contact name aman",
                "search contact by name aman",
            ],
            original_query=kwargs["query"],
            failure_reason=kwargs["failure_reason"],
            react_trace=kwargs.get("react_trace"),
        )

    async_mock: AsyncMock = AsyncMock(side_effect=build_mock)
    monkeypatch.setattr(react_agent, "build_clarification", async_mock)
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
