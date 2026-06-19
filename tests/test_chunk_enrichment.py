from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nl2sql_service.rag import chunker
from nl2sql_service.db import schema_loader
from nl2sql_service.core.config import settings


@pytest.mark.asyncio
async def test_columns_loaded_appear_in_chunk_text(
    monkeypatch: pytest.MonkeyPatch,
    mock_load_columns_for_ingest: AsyncMock,
    mock_entity_with_enrichment: dict,
):
    monkeypatch.setattr(schema_loader, "get_entity", lambda _group: mock_entity_with_enrichment)
    monkeypatch.setattr(schema_loader, "get_schema_version", lambda _group: "abcd1234")

    chunk = await chunker.chunk_schema_group("billing", settings)

    assert "status" in chunk["text"]
    assert "due_date" in chunk["text"]
    assert "total_amount" in chunk["text"]
    assert chunk["has_columns"] is True
    assert chunk["column_source"] == "mysql_live"
    assert mock_load_columns_for_ingest.await_count == 1


@pytest.mark.asyncio
async def test_mysql_unavailable_chunk_still_builds(
    monkeypatch: pytest.MonkeyPatch,
    mock_load_columns_unavailable: AsyncMock,
    mock_entity_with_enrichment: dict,
):
    monkeypatch.setattr(schema_loader, "get_entity", lambda _group: mock_entity_with_enrichment)
    monkeypatch.setattr(schema_loader, "get_schema_version", lambda _group: "abcd1234")

    chunk = await chunker.chunk_schema_group("billing", settings)

    assert chunk
    assert "(columns unavailable)" in chunk["text"]
    assert chunk["has_columns"] is False
    assert chunk["column_source"] == "unavailable"
    assert mock_load_columns_unavailable.await_count == 1


@pytest.mark.asyncio
async def test_business_aliases_in_chunk_text(
    monkeypatch: pytest.MonkeyPatch,
    mock_load_columns_for_ingest: AsyncMock,
    mock_entity_with_enrichment: dict,
):
    monkeypatch.setattr(schema_loader, "get_entity", lambda _group: mock_entity_with_enrichment)
    monkeypatch.setattr(schema_loader, "get_schema_version", lambda _group: "abcd1234")

    chunk = await chunker.chunk_schema_group("billing", settings)

    assert "counselor" in chunk["text"]
    assert "client" in chunk["text"]
    assert chunk["has_aliases"] is True


@pytest.mark.asyncio
async def test_column_aliases_are_embedded_from_introspection(
    monkeypatch: pytest.MonkeyPatch,
):
    entity = {
        "entity_id": "contacts",
        "chunk_group_name": "Contacts",
        "root_table": "contact",
        "root_table_ref": "contact",
        "included_tables": [],
        "summarized_tables": [],
        "referenced_tables": [],
        "excluded_tables": [],
        "relation_ids": [],
        "chunking_rules": [],
        "rationale": "Contact directory",
        "table_ref_map": {},
        "secondary_memberships": [],
    }

    async def _columns(*args, **kwargs):
        return {"contact": ["first_name", "mobile_number"]}

    monkeypatch.setattr(schema_loader, "get_entity", lambda _group: entity)
    monkeypatch.setattr(schema_loader, "get_schema_version", lambda _group: "abcd1234")
    monkeypatch.setattr(chunker, "load_columns_for_tables", _columns)

    chunk = await chunker.chunk_schema_group("contacts", settings)

    assert "first name" in chunk["text"]
    assert "mobile number" in chunk["text"]


@pytest.mark.asyncio
async def test_no_alias_section_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    mock_load_columns_for_ingest: AsyncMock,
    mock_entity_no_enrichment: dict,
):
    monkeypatch.setattr(schema_loader, "get_entity", lambda _group: mock_entity_no_enrichment)
    monkeypatch.setattr(schema_loader, "get_schema_version", lambda _group: "abcd1234")

    chunk = await chunker.chunk_schema_group("billing", settings)

    assert "Business terms:" not in chunk["text"]
    assert chunk["has_aliases"] is False


@pytest.mark.asyncio
async def test_example_questions_in_chunk_text(
    monkeypatch: pytest.MonkeyPatch,
    mock_load_columns_for_ingest: AsyncMock,
    mock_entity_with_enrichment: dict,
):
    monkeypatch.setattr(schema_loader, "get_entity", lambda _group: mock_entity_with_enrichment)
    monkeypatch.setattr(schema_loader, "get_schema_version", lambda _group: "abcd1234")

    chunk = await chunker.chunk_schema_group("billing", settings)

    assert "show unpaid invoices by counselor" in chunk["text"]
    assert "total revenue this month" in chunk["text"]
    assert chunk["has_examples"] is True


@pytest.mark.asyncio
async def test_no_examples_section_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    mock_load_columns_for_ingest: AsyncMock,
    mock_entity_no_enrichment: dict,
):
    monkeypatch.setattr(schema_loader, "get_entity", lambda _group: mock_entity_no_enrichment)
    monkeypatch.setattr(schema_loader, "get_schema_version", lambda _group: "abcd1234")

    chunk = await chunker.chunk_schema_group("billing", settings)

    assert "Example questions:" not in chunk["text"]
    assert chunk["has_examples"] is False


@pytest.mark.asyncio
async def test_token_guard_raises_for_oversized_chunk(
    monkeypatch: pytest.MonkeyPatch,
):
    oversized_entity = {
        "entity_id": "billing",
        "chunk_group_name": "Billing",
        "root_table": "invoice",
        "root_table_ref": "invoice",
        "included_tables": ["payment", "member", "employee"],
        "summarized_tables": ["contact"],
        "referenced_tables": ["followup", "inquiry"],
        "excluded_tables": [],
        "relation_ids": ["payment.invoice_id->invoice.id"],
        "chunking_rules": ["max_tokens:400"],
        "rationale": "Billing and payment tracking",
        "secondary_memberships": [],
        "table_ref_map": {},
        "business_aliases": {
            "invoice": ["bill", "fee", "charge", "dues", "invoice_record"],
            "member": ["client", "patient", "student", "subscriber"],
            "employee": ["counselor", "advisor", "staff", "agent"],
            "payment": ["receipt", "transaction", "settlement", "collection"],
        },
        "example_questions": [
            "show unpaid invoices by counselor for last thirty days with amounts and payment references",
            "list overdue members and invoice balances with responsible employee and followup plan",
            "which invoices remain unpaid after partial payments and what is the pending amount per member",
            "summarize payment trends by counselor and member segment for this quarter with invoice-level details",
            "find members with repeated overdue invoices and latest followup outcome and assigned staff",
            "show invoice aging buckets and payment lag by employee and branch for active members",
            "identify invoices without payment receipts and list related inquiry records and followup status",
            "return high-value unpaid invoices with member contact readiness and counselor assignment",
            "show historical unpaid invoice streaks and payment attempts by member and counselor",
            "report unresolved invoice balances grouped by due date windows and staff ownership",
        ],
    }
    many_columns = {
        table: [f"col_{i}" for i in range(60)]
        for table in ["invoice", "payment", "member", "employee"]
    }

    async def _mock_columns(*args, **kwargs):
        return many_columns

    monkeypatch.setattr(schema_loader, "get_entity", lambda _group: oversized_entity)
    monkeypatch.setattr(schema_loader, "get_schema_version", lambda _group: "abcd1234")
    monkeypatch.setattr(chunker, "load_columns_for_tables", _mock_columns)

    with pytest.raises(ValueError) as exc:
        await chunker.chunk_schema_group("billing", settings)

    message = str(exc.value)
    assert "Exceeds 400 limit" in message
    assert "billing" in message


@pytest.mark.asyncio
async def test_ingest_groups_response_includes_enrichment_summary(
    client,
    monkeypatch: pytest.MonkeyPatch,
    mock_load_columns_for_ingest: AsyncMock,
):
    from nl2sql_service.rag import ingest

    del mock_load_columns_for_ingest

    async_mock = AsyncMock(
        return_value={
            "inserted_count": 0,
            "updated_count": 8,
            "enrichment_summary": {
                "groups_with_columns": 8,
                "groups_without_columns": 0,
                "groups_with_aliases": 6,
                "groups_with_examples": 8,
            },
        }
    )
    monkeypatch.setattr(ingest, "ingest_schema_groups", async_mock)

    response = await client.post("/ingest/groups", json={})
    body = response.json()

    assert response.status_code == 200
    assert "enrichment_summary" in body
    assert body["enrichment_summary"]["groups_with_columns"] > 0
    assert body["enrichment_summary"]["groups_with_examples"] >= 0


@pytest.mark.asyncio
async def test_schema_version_unchanged_when_only_mysql_changes(
    tmp_rag_schema,
    monkeypatch: pytest.MonkeyPatch,
):
    del tmp_rag_schema

    async def _columns_v1(*args, **kwargs):
        return {"invoice": ["id", "status"]}

    async def _columns_v2(*args, **kwargs):
        return {"invoice": ["id", "status", "due_date"]}

    monkeypatch.setattr(chunker, "load_columns_for_tables", _columns_v1)
    v1 = schema_loader.get_schema_version("billing")

    monkeypatch.setattr(chunker, "load_columns_for_tables", _columns_v2)
    v2 = schema_loader.get_schema_version("billing")

    assert v1 == v2


@pytest.mark.asyncio
async def test_live_column_catalog_chunks_include_semantic_aliases(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        schema_loader,
        "load_column_catalog",
        AsyncMock(
            return_value=[
                {
                    "table_name": "contact",
                    "column_name": "first_name",
                    "data_type": "varchar",
                    "ordinal_position": 1,
                }
            ]
        ),
    )

    chunks = await schema_loader.load_live_column_catalog_chunks(settings)

    assert len(chunks) == 1
    assert chunks[0]["column_aliases"]
    assert "first name" in chunks[0]["text"]
