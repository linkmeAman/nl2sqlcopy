from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nl2sql_service.storage import instruction_store
from nl2sql_service.agent import react_agent, react_executor, react_planner
from nl2sql_service.rag import retrieve
from nl2sql_service.core.config import settings


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


class _InstructionConn:
    def __init__(self, instructions: list[dict] | None = None) -> None:
        self.instructions = instructions or []
        self.embedding_updates: list[str] = []
        self.next_id = max([item["id"] for item in self.instructions], default=0) + 1
        self.cache_epoch = 1
        self.pending_confirmations: dict[str, dict] = {}

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        if "SELECT COUNT(*)::bigint AS pending_count" in sql:
            return [{"pending_count": len(self.pending_confirmations)}]

        if "SELECT id, confidence_score, failure_count" in sql:
            tables = set(args[0])
            return [
                {
                    "id": item["id"],
                    "confidence_score": item["confidence_score"],
                    "failure_count": item["failure_count"],
                }
                for item in self.instructions
                if item.get("is_active", True)
                and tables.intersection(item.get("tables_affected", []))
            ]

        if "WHERE is_active = TRUE" in sql and "confidence_score >= $1" in sql:
            min_confidence = float(args[0])
            return [
                item
                for item in self.instructions
                if item.get("is_active", True)
                and float(item.get("confidence_score", 0.0)) >= min_confidence
            ]

        if "WHERE ($1::text IS NULL OR instruction_type = $1)" in sql:
            instruction_type = args[0]
            active_only = args[1]
            return [
                item
                for item in self.instructions
                if (instruction_type is None or item["instruction_type"] == instruction_type)
                and (not active_only or item.get("is_active", True))
            ]

        if "content ILIKE" in sql:
            return []

        return []

    async def fetchrow(self, sql: str, *args: Any) -> dict | None:
        if "DELETE FROM nl2sql_pending_teach_confirmations" in sql and "RETURNING instruction, conflicting_id, created_at" in sql:
            token = args[0]
            pending = self.pending_confirmations.get(token)
            if pending and pending["expires_at"] > _now_like(pending["expires_at"]):
                self.pending_confirmations.pop(token, None)
                return {
                    "instruction": pending["instruction"],
                    "conflicting_id": pending["conflicting_id"],
                    "created_at": pending["created_at"],
                }
            return None

        if "SELECT COUNT(*)::bigint AS pending_count" in sql:
            return {"pending_count": len(self.pending_confirmations)}

        if "UPDATE nl2sql_cache_state" in sql:
            self.cache_epoch += 1
            return {"cache_epoch": self.cache_epoch}

        if "INSERT INTO nl2sql_user_instructions" not in sql:
            return None

        instruction_id = self.next_id
        self.next_id += 1
        self.instructions.append(
            {
                "id": instruction_id,
                "instruction_type": args[0],
                "content": args[1],
                "embedding_source": args[2],
                "instruction_embedding": args[3],
                "tables_affected": list(args[4] or []),
                "confidence_score": float(args[5]),
                "is_verified": bool(args[6]),
                "is_active": True,
                "conflict_group": args[8],
                "source_query": args[7],
                "use_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "last_used_at": None,
                "created_at": datetime(2026, 4, 29, 10, 0, 0),
                "updated_at": datetime(2026, 4, 29, 10, 0, 0),
            }
        )
        return {"id": instruction_id}

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO nl2sql_pending_teach_confirmations" in sql:
            token = args[0]
            self.pending_confirmations[token] = {
                "instruction": json.loads(args[1]),
                "conflicting_id": args[2],
                "expires_at": args[3],
                "created_at": datetime(2026, 4, 29, 10, 0, 0, tzinfo=args[3].tzinfo),
            }
            return "INSERT 0 1"

        if "DELETE FROM nl2sql_pending_teach_confirmations" in sql and "WHERE expires_at <= NOW()" in sql and "ORDER BY created_at ASC" not in sql:
            removed = 0
            for token, pending in list(self.pending_confirmations.items()):
                if pending["expires_at"] <= _now_like(pending["expires_at"]):
                    self.pending_confirmations.pop(token, None)
                    removed += 1
            return f"DELETE {removed}"

        if "DELETE FROM nl2sql_pending_teach_confirmations" in sql and "ORDER BY created_at ASC" in sql:
            limit = args[0]
            ordered = sorted(
                self.pending_confirmations.items(),
                key=lambda item: item[1]["created_at"],
            )
            removed = 0
            for token, _pending in ordered[:limit]:
                self.pending_confirmations.pop(token, None)
                removed += 1
            return f"DELETE {removed}"

        if "DELETE FROM nl2sql_pending_teach_confirmations" in sql and "AND expires_at <= NOW()" in sql:
            token = args[0]
            pending = self.pending_confirmations.get(token)
            if pending and pending["expires_at"] <= _now_like(pending["expires_at"]):
                self.pending_confirmations.pop(token, None)
                return "DELETE 1"
            return "DELETE 0"

        if "INSERT INTO nl2sql_cache_state" in sql:
            return "INSERT 0 1"

        if "failure_count = failure_count + 1" in sql:
            instruction_id = args[0]
            new_confidence = float(args[1])
            for item in self.instructions:
                if item["id"] == instruction_id:
                    item["use_count"] += 1
                    item["failure_count"] += 1
                    item["confidence_score"] = new_confidence
            return "UPDATE 1"

        if "success_count = success_count + 1" in sql:
            instruction_id = args[0]
            for item in self.instructions:
                if item["id"] == instruction_id:
                    item["use_count"] += 1
                    item["success_count"] += 1
            return "UPDATE 1"

        if "UPDATE nl2sql_user_instructions" in sql and "is_active = FALSE" in sql:
            instruction_id = args[0]
            for item in self.instructions:
                if item["id"] == instruction_id:
                    item["is_active"] = False
            return "UPDATE 1"

        if "UPDATE nl2sql_embeddings" in sql:
            self.embedding_updates.append(args[0])
            return "UPDATE 1"

        return "UPDATE 0"


def _now_like(sample: datetime) -> datetime:
    return datetime.now(tz=sample.tzinfo)


def _instruction(
    instruction_id: int,
    instruction_type: str = "table_relationship",
    content: str = "employee.contact_id = contact.id",
    tables: list[str] | None = None,
    confidence: float = 1.0,
    verified: bool = True,
    failure_count: int = 0,
) -> dict:
    tables = tables or ["employee", "contact"]
    return {
        "id": instruction_id,
        "instruction_type": instruction_type,
        "content": content,
        "embedding_source": instruction_store.build_embedding_source(
            instruction_type,
            content,
            tables,
        ),
        "tables_affected": tables,
        "confidence_score": confidence,
        "is_verified": verified,
        "is_active": True,
        "conflict_group": None,
        "source_query": None,
        "use_count": 0,
        "success_count": 0,
        "failure_count": failure_count,
        "last_used_at": None,
        "created_at": datetime(2026, 4, 29, 10, 0, 0),
        "updated_at": datetime(2026, 4, 29, 10, 0, 0),
    }


@pytest.fixture(autouse=True)
def clear_pending_instructions() -> None:
    instruction_store._pending_instructions.clear()


@pytest.mark.asyncio
async def test_teach_new_table_relationship_saved(
    app,
    client,
    mock_detect_conflict_none,
) -> None:
    del mock_detect_conflict_none
    app.state.pool = _FakePool(_InstructionConn())

    response = await client.post(
        "/teach",
        json={
            "instruction_type": "table_relationship",
            "content": "employee.contact_id = contact.id",
            "tables_affected": ["employee", "contact"],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["learning_status"] == "saved_new"
    assert body["instruction_id"] is not None
    assert "new" in body["message"].lower()


@pytest.mark.asyncio
async def test_teach_conflict_detected(
    app,
    client,
    mock_detect_conflict_found,
) -> None:
    del mock_detect_conflict_found
    app.state.pool = _FakePool(_InstructionConn())

    response = await client.post(
        "/teach",
        json={
            "instruction_type": "table_relationship",
            "content": "employee.employee_id = contact.id",
            "tables_affected": ["employee", "contact"],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["learning_status"] == "conflict_detected"
    assert body["requires_confirmation"] is True
    assert body["confirmation_token"] is not None
    assert len(body["similar_instructions"]) >= 1


@pytest.mark.asyncio
async def test_teach_confirm_replace(
    app,
    client,
    mock_detect_conflict_found,
) -> None:
    del mock_detect_conflict_found
    conn = _InstructionConn([_instruction(10)])
    app.state.pool = _FakePool(conn)
    conflict_response = await client.post(
        "/teach",
        json={
            "instruction_type": "table_relationship",
            "content": "employee.employee_id = contact.id",
            "tables_affected": ["employee", "contact"],
        },
    )
    token = conflict_response.json()["confirmation_token"]

    response = await client.post(
        "/teach/confirm",
        json={"confirmation_token": token, "action": "replace"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["learning_status"] == "confirmed"
    assert "replaced" in body["message"].lower()


@pytest.mark.asyncio
async def test_teach_confirm_replace_survives_in_memory_clear(
    app,
    client,
    mock_detect_conflict_found,
) -> None:
    del mock_detect_conflict_found
    conn = _InstructionConn([_instruction(10)])
    app.state.pool = _FakePool(conn)
    conflict_response = await client.post(
        "/teach",
        json={
            "instruction_type": "table_relationship",
            "content": "employee.employee_id = contact.id",
            "tables_affected": ["employee", "contact"],
        },
    )
    token = conflict_response.json()["confirmation_token"]

    instruction_store._pending_instructions.clear()

    response = await client.post(
        "/teach/confirm",
        json={"confirmation_token": token, "action": "replace"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["learning_status"] == "confirmed"
    assert "replaced" in body["message"].lower()


@pytest.mark.asyncio
async def test_teach_confirm_expired_token(app, client) -> None:
    app.state.pool = _FakePool(_InstructionConn())

    response = await client.post(
        "/teach/confirm",
        json={"confirmation_token": "deadbeef", "action": "replace"},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["learning_status"] == "rejected"
    assert "expired" in body["message"].lower()


@pytest.mark.asyncio
async def test_instructions_injected_into_context(
    mock_embed,
    mock_instruction_store_with_rules,
    mock_pattern_store_empty,
) -> None:
    del mock_embed, mock_instruction_store_with_rules, mock_pattern_store_empty

    result = await retrieve.retrieve_groups(
        query="fetch counselor contact",
        top_k=3,
        pool=_FakePool(_GroupConn()),
    )
    context = result.context

    assert "USER-PROVIDED RULES" in context
    assert "counselor means employee" in context
    assert "employee.contact_id = contact.id" in context
    assert context.index("USER-PROVIDED RULES") < context.index("## Schema group")


@pytest.mark.asyncio
async def test_instructions_not_injected_when_empty(
    mock_embed,
    mock_instruction_store_empty,
    mock_pattern_store_empty,
) -> None:
    del mock_embed, mock_instruction_store_empty, mock_pattern_store_empty

    result = await retrieve.retrieve_groups(
        query="fetch counselor contact",
        top_k=3,
        pool=_FakePool(_GroupConn()),
    )

    assert "USER-PROVIDED RULES" not in result.context


@pytest.mark.asyncio
async def test_teach_term_mapping_similar_found(
    app,
    client,
    monkeypatch: pytest.MonkeyPatch,
    mock_detect_conflict_none,
) -> None:
    del mock_detect_conflict_none
    app.state.pool = _FakePool(_InstructionConn())
    monkeypatch.setattr(
        instruction_store,
        "find_similar_instructions",
        AsyncMock(
            return_value=[
                {
                    "id": 7,
                    "instruction_type": "term_mapping",
                    "content": "counselor means employee table",
                    "confidence_score": 0.9,
                    "is_verified": True,
                    "use_count": 3,
                }
            ]
        ),
    )

    response = await client.post(
        "/teach",
        json={
            "instruction_type": "term_mapping",
            "content": "staff also means employee table",
            "tables_affected": ["employee"],
        },
    )

    body = response.json()
    assert body["learning_status"] == "similar_found"
    assert len(body["similar_instructions"]) >= 1
    assert body["instruction_id"] is not None


@pytest.mark.asyncio
async def test_teach_correction_type(app, client) -> None:
    app.state.pool = _FakePool(_InstructionConn())

    response = await client.post(
        "/teach",
        json={
            "instruction_type": "correction",
            "content": (
                "previous join was wrong. Use contact.id = employee.contact_id "
                "not employee.id"
            ),
            "tables_affected": ["employee", "contact"],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["learning_status"] in ["saved_new", "conflict_detected"]
    assert body["instruction_id"] is not None or body["requires_confirmation"] is True


@pytest.mark.asyncio
async def test_ingest_instructions_embeds_active_ones(
    app,
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import main

    conn = _InstructionConn(
        [
            _instruction(1),
            _instruction(
                2,
                instruction_type="term_mapping",
                content="counselor means employee table",
                tables=["employee"],
                confidence=0.9,
            ),
        ]
    )
    app.state.pool = _FakePool(conn)

    async def fake_upsert(chunks: list[dict], pool: object) -> dict[str, int]:
        del pool
        return {"inserted_count": len(chunks), "updated_count": 0}

    monkeypatch.setattr(main.ingest, "_upsert_versioned_chunks", AsyncMock(side_effect=fake_upsert))

    response = await client.post("/ingest/instructions")

    body = response.json()
    assert response.status_code == 200
    assert body["embedded"] >= 2
    assert body["source"] == "user_instructions"


@pytest.mark.asyncio
async def test_get_instructions_returns_list(app, client) -> None:
    app.state.pool = _FakePool(_InstructionConn([_instruction(1)]))

    response = await client.get("/instructions")

    body = response.json()
    assert response.status_code == 200
    assert isinstance(body, list)
    assert {"id", "instruction_type", "content", "confidence_score", "is_verified"}.issubset(
        body[0]
    )


@pytest.mark.asyncio
async def test_delete_instruction_deactivates(app, client) -> None:
    conn = _InstructionConn([_instruction(1)])
    app.state.pool = _FakePool(conn)

    response = await client.delete("/instructions/1")

    assert response.status_code == 200
    assert response.json() == {"deactivated": True, "instruction_id": 1}
    assert conn.instructions[0]["is_active"] is False


@pytest.mark.asyncio
async def test_record_instruction_outcome_decays_confidence() -> None:
    conn = _InstructionConn(
        [
            _instruction(
                1,
                tables=["employee"],
                confidence=0.7,
                failure_count=2,
            )
        ]
    )

    await instruction_store.record_instruction_outcome(
        tables_used=["employee"],
        success=False,
        pool=_FakePool(conn),
    )

    assert conn.instructions[0]["failure_count"] == 3
    assert conn.instructions[0]["confidence_score"] < 0.7


def test_verified_instructions_take_priority_in_prompt() -> None:
    unverified = {
        "instruction_type": "business_rule",
        "content": "Unverified rule",
        "confidence_score": 0.7,
        "is_verified": False,
    }
    verified = {
        "instruction_type": "business_rule",
        "content": "Verified rule",
        "confidence_score": 1.0,
        "is_verified": True,
    }

    rendered = instruction_store.format_instructions_for_prompt([unverified, verified])

    assert rendered.index("Verified rule") < rendered.index("Unverified rule")


@pytest.mark.asyncio
async def test_instructions_appear_before_patterns(
    mock_embed,
    mock_instruction_store_with_rules,
    mock_pattern_store_with_join_pattern,
) -> None:
    del mock_embed, mock_instruction_store_with_rules, mock_pattern_store_with_join_pattern

    result = await retrieve.retrieve_groups(
        query="fetch counselor contact",
        top_k=3,
        pool=_FakePool(_GroupConn()),
    )
    context = result.context

    assert context.index("USER-PROVIDED RULES") < context.index("PREVIOUSLY LEARNED PATTERNS")


@pytest.mark.asyncio
async def test_give_up_triggers_confidence_decay(
    mock_embed,
    monkeypatch: pytest.MonkeyPatch,
    mock_instruction_store_with_rules,
    mock_build_clarification,
) -> None:
    del mock_instruction_store_with_rules, mock_build_clarification
    monkeypatch.setattr(
        react_executor,
        "retrieve_groups",
        AsyncMock(
            return_value={
                "matched_groups": ["inquiry_lifecycle"],
                "tables_in_scope": ["employee", "contact"],
                "context": "USER-PROVIDED RULES\nemployee.contact_id = contact.id",
                "results": [],
            }
        ),
    )
    monkeypatch.setattr(
        react_executor,
        "load_columns_for_tables",
        AsyncMock(return_value={"employee": ["id", "contact_id"], "contact": ["id"]}),
    )
    monkeypatch.setattr(
        react_planner,
        "call_reasoning_model",
        AsyncMock(return_value=("Cannot continue", "ACTION: GIVE_UP\nINPUT: no match", [])),
    )
    outcome_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(react_executor, "record_instruction_outcome", outcome_mock)

    await react_executor.run(
        query="fetch counselor",
        pool=_FakePool(_InstructionConn()),
        settings=settings,
    )
    await asyncio.sleep(0)

    assert outcome_mock.called
    assert outcome_mock.call_args.kwargs["success"] is False
