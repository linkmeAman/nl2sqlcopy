from __future__ import annotations

from scripts import nl2sql_smoke_test


def test_expect_path_value_accepts_nested_match() -> None:
    ok, note = nl2sql_smoke_test._expect_path_value(
        {"provider_config": {"status": "ok"}},
        "provider_config.status",
        "ok",
    )

    assert ok is True
    assert note == "ok"


def test_expect_path_value_rejects_missing_path() -> None:
    ok, note = nl2sql_smoke_test._expect_path_value(
        {"provider_config": {}},
        "provider_config.status",
        "ok",
    )

    assert ok is False
    assert "missing path" in note
