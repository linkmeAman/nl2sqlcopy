from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nl2sql_service.config import settings
from nl2sql_service.models import WarningCode
from nl2sql_service.rulebook import RulebookConfig, build_governance_block


def test_build_governance_block_orders_rules_by_category() -> None:
    block = build_governance_block(RulebookConfig(), context="react")

    query_safety_index = block.index("QUERY SAFETY (HARD RULE):")
    schema_fidelity_index = block.index("SCHEMA FIDELITY (HARD RULE):")
    uncertainty_index = block.index("UNCERTAINTY DECLARATION (HARD RULE):")
    answer_grounding_index = block.index("ANSWER GROUNDING (HARD RULE):")
    self_verification_index = block.index("SELF-VERIFICATION (REQUIRED BEFORE RETURNING):")

    assert query_safety_index < schema_fidelity_index
    assert schema_fidelity_index < uncertainty_index
    assert uncertainty_index < answer_grounding_index
    assert answer_grounding_index < self_verification_index


def test_build_governance_block_filters_to_sql_generation_rules() -> None:
    block = build_governance_block(RulebookConfig(), context="sql_gen")

    assert "QUERY SAFETY (HARD RULE):" in block
    assert "SCHEMA FIDELITY (HARD RULE):" in block
    assert "SELF-VERIFICATION (REQUIRED BEFORE RETURNING):" in block
    assert "ANSWER GROUNDING (HARD RULE):" not in block
    assert "UNCERTAINTY DECLARATION (HARD RULE):" not in block


@pytest.mark.asyncio
async def test_governance_rules_endpoint_returns_503_when_disabled(client) -> None:
    response = await client.get("/governance/rules")

    assert response.status_code == 503
    assert response.json()["detail"] == "Governance disabled"


@pytest.mark.asyncio
async def test_governance_rules_endpoint_returns_active_rules(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import rulebook

    monkeypatch.setattr(settings, "governance_enabled", True)
    monkeypatch.setattr(rulebook, "_config", None)

    response = await client.get("/governance/rules")

    assert response.status_code == 200
    body = response.json()
    assert body["total_rules"] == 10
    assert body["enabled_rules"] == 10
    assert len(body["rules"]) == 10
    assert any(rule["name"] == "schema_fidelity" for rule in body["rules"])


@pytest.mark.asyncio
async def test_governance_validate_endpoint_uses_review_gate(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import main, rulebook

    monkeypatch.setattr(settings, "governance_enabled", True)
    monkeypatch.setattr(rulebook, "_config", None)
    monkeypatch.setattr(
        main,
        "load_columns_for_tables",
        AsyncMock(return_value={"invoice": ["id", "status"]}),
    )
    monkeypatch.setattr(
        main,
        "review_sql",
        AsyncMock(return_value=(False, ["2", "5"])),
    )

    response = await client.post(
        "/governance/validate",
        json={
            "sql": "SELECT id FROM invoice WHERE status='unpaid'",
            "query": "show unpaid invoices",
            "tables_in_scope": ["invoice"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["passes"] is False
    assert body["violations"] == ["2", "5"]


@pytest.mark.asyncio
async def test_generate_sql_adds_review_failed_warning_when_gate_flags(
    client,
    mock_ollama,
    mock_retrieve_groups,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import rulebook, sql_generator

    del mock_retrieve_groups
    monkeypatch.setattr(settings, "governance_enabled", True)
    monkeypatch.setattr(rulebook, "_config", None)
    monkeypatch.setattr(
        sql_generator,
        "load_columns_for_tables",
        AsyncMock(return_value={"invoice": ["id", "amount", "status"]}),
    )
    monkeypatch.setattr(
        sql_generator,
        "review_sql",
        AsyncMock(return_value=(False, ["2", "5"])),
    )

    response = await client.post(
        "/generate-sql",
        json={"query": "show unpaid invoices"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert any(
        warning["code"] == WarningCode.REVIEW_FAILED.value
        for warning in body["warnings"]
    )
