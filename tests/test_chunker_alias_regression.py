from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nl2sql_service import chunker
from nl2sql_service.config import settings


@pytest.mark.asyncio
async def test_chunk_schema_group_preserves_business_alias_dict_during_column_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity = {
        "entity_id": "billing",
        "chunk_group_name": "billing",
        "root_table": "invoice",
        "root_table_ref": "invoice",
        "included_tables": ["payment"],
        "summarized_tables": [],
        "referenced_tables": ["member"],
        "excluded_tables": [],
        "relation_ids": [],
        "rationale": "Billing context",
        "secondary_memberships": [],
        "table_ref_map": {},
    }
    monkeypatch.setattr(chunker.schema_loader, "get_entity", lambda group_name: entity)
    monkeypatch.setattr(chunker.schema_loader, "get_business_aliases", lambda group_name: {"invoice": ["bill"]})
    monkeypatch.setattr(chunker.schema_loader, "get_example_questions", lambda group_name: [])
    monkeypatch.setattr(chunker.schema_loader, "get_schema_version", lambda group_name: "abc12345")
    monkeypatch.setattr(
        chunker,
        "load_columns_for_tables",
        AsyncMock(return_value={"invoice": ["created_at"], "payment": ["date"]}),
    )

    chunk = await chunker.chunk_schema_group(
        group_name="billing",
        settings=settings,
        allowed_columns=None,
    )

    assert chunk["source"] == "billing"
    assert chunk["has_aliases"] is True
    assert "Business terms:" in chunk["text"]
