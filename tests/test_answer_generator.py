from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nl2sql_service import answer_generator
from nl2sql_service.config import settings
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


def test_answer_prompt_is_concise_and_uses_selected_columns():
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
    )

    assert "Answer in at most 80 words." in prompt
    assert "Result rows (first 10 rows, selected columns):" in prompt
    assert "Displayed columns:" in prompt
    assert "balance" not in prompt
    assert "created_by" not in prompt
    assert "... 2 more rows" in prompt


@pytest.mark.asyncio
async def test_call_answer_model_disables_thinking_and_caps_tokens(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeResponse:
        is_success = True

        def json(self) -> dict:
            return {"response": "Here are the matching rows."}

    class FakeClient:
        def __init__(self, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, json: dict) -> FakeResponse:
            del url
            assert json["think"] is False
            assert json["options"]["num_predict"] == 300
            assert json["options"]["temperature"] == settings.answer_temperature
            return FakeResponse()

    monkeypatch.setattr(answer_generator.httpx, "AsyncClient", FakeClient)

    answer, warnings = await answer_generator.call_answer_model(
        prompt="summarize rows",
        settings=settings,
    )

    assert warnings == []
    assert answer == "Here are the matching rows."
