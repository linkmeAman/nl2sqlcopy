from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nl2sql_service import answer_generator
from nl2sql_service.config import settings
from nl2sql_service.model_client import ModelResponse
from nl2sql_service.models import SqlWarning, WarningCode


@pytest.mark.asyncio
async def test_generate_answer_falls_back_when_answer_model_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    warning = SqlWarning(
        code=WarningCode.ANSWER_TIMEOUT,
        message="Answer model timed out",
    )
    monkeypatch.setattr(
        answer_generator,
        "call_answer_model",
        AsyncMock(return_value=(None, [warning])),
    )

    answer, warnings = await answer_generator.generate_answer(
        query="show me the 5 most recent payments",
        sql="SELECT * FROM payment ORDER BY created_at DESC LIMIT 5",
        columns=[
            "id",
            "invoice_id",
            "date",
            "amount",
            "calculated_amount",
            "actual_amount",
            "balance",
            "receipt",
            "pay_mode",
            "created_at",
        ],
        rows=[
            (10, 20, "2026-04-27", 100, 100, 100, 0, "R-10", "card", "2026-04-27 10:00:00"),
            (9, 19, "2026-04-26", 80, 80, 80, 0, "R-9", "cash", "2026-04-26 10:00:00"),
        ],
        row_count=2,
        sql_warnings=[],
        settings=settings,
    )

    assert warnings == [warning]
    assert answer is not None
    assert answer.startswith("Found 2 rows.")
    assert "invoice_id" in answer
    assert "amount" in answer
    assert "created_at" in answer


def test_answer_prompt_uses_structured_template_rows():
    columns = [
        "id",
        "contact_id",
        "type",
        "source",
        "heard_from",
        "balance",
        "created_by",
        "created_at",
    ]
    rows = [
        (index, 1000 + index, "lead", "web", "instagram", 0, 7, f"2026-04-{index:02d}")
        for index in range(1, 13)
    ]

    prompt = answer_generator.build_answer_prompt(
        query="show me the 5 most recent inquiries",
        sql="SELECT * FROM inquiry ORDER BY created_at DESC LIMIT 5",
        columns=columns,
        rows=rows,
        row_count=len(rows),
        warnings=[],
        settings=settings,
    )

    assert "ANSWER: <one sentence directly answering the question>" in prompt
    assert "KEY FIGURES:" in prompt
    assert "DETAILS:" in prompt
    assert "DATA (12 rows):" in prompt
    assert "Columns: id, contact_id, type, source, heard_from, balance, created_by, created_at" in prompt
    assert "Row 1: id=1, contact_id=1001" in prompt
    assert "Row 10: id=10, contact_id=1010" in prompt
    assert "Row 11:" not in prompt


@pytest.mark.asyncio
async def test_call_answer_model_disables_thinking_and_caps_tokens(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeClient:
        provider_name = "fake"

        async def generate(self, **kwargs):
            assert kwargs["enable_thinking"] is settings.answer_allow_reasoning
            assert kwargs["max_tokens"] == settings.answer_max_tokens
            assert kwargs["temperature"] == settings.answer_temperature
            return ModelResponse(text="Here are the matching rows.")

    monkeypatch.setattr(
        answer_generator,
        "get_model_client",
        lambda **kwargs: FakeClient(),
    )

    answer, warnings = await answer_generator.call_answer_model(
        prompt="summarize rows",
        settings=settings,
    )

    assert warnings == []
    assert answer == "Here are the matching rows."


def test_enforce_answer_style_removes_narrative_prefix_and_truncates_words():
    class _Settings:
        answer_strict_concise = True
        answer_max_words = 5

    result = answer_generator._enforce_answer_style(
        "Okay, let's tackle this. Here are the latest payment rows for the account owner",
        _Settings(),
    )
    assert result == "Here are the latest payment"


@pytest.mark.asyncio
async def test_generate_answer_parses_template_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        answer_generator,
        "call_answer_model",
        AsyncMock(
            return_value=(
                "ANSWER: The latest payment is R-10.\n"
                "KEY FIGURES: amount 100\n"
                "DETAILS: receipt R-10",
                [],
            )
        ),
    )

    answer, warnings = await answer_generator.generate_answer(
        query="newest payment",
        sql="SELECT receipt, amount FROM payment",
        columns=["receipt", "amount"],
        rows=[("R-10", 100)],
        row_count=1,
        sql_warnings=[],
        settings=settings,
    )

    assert warnings == []
    assert answer == "The latest payment is R-10. Key figures: amount 100. Details: receipt R-10."


@pytest.mark.asyncio
async def test_generate_answer_falls_back_when_template_parse_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        answer_generator,
        "call_answer_model",
        AsyncMock(return_value=("This is not the requested template.", [])),
    )

    answer, warnings = await answer_generator.generate_answer(
        query="show payments",
        sql="SELECT id, amount FROM payment",
        columns=["id", "amount"],
        rows=[(1, 100)],
        row_count=1,
        sql_warnings=[],
        settings=settings,
    )

    assert warnings == []
    assert answer.startswith("Found 1 row.")


def test_validate_answer_numbers_reports_numbers_not_in_rows() -> None:
    violations = answer_generator.validate_answer_numbers(
        "There are 3 rows and amount 100.",
        [{"id": 1, "amount": 100}],
    )

    assert violations == ["Number '3' in answer not found in data"]


@pytest.mark.asyncio
async def test_generate_answer_warns_on_invented_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        answer_generator,
        "call_answer_model",
        AsyncMock(
            return_value=(
                "ANSWER: The result amount is 999.\n"
                "KEY FIGURES: none\n"
                "DETAILS: none",
                [],
            )
        ),
    )

    answer, warnings = await answer_generator.generate_answer(
        query="show payment amount",
        sql="SELECT amount FROM payment",
        columns=["amount"],
        rows=[(100,)],
        row_count=1,
        sql_warnings=[],
        settings=settings,
    )

    assert answer == "The result amount is 999."
    assert warnings[0].code == WarningCode.ANSWER_HALLUCINATION
