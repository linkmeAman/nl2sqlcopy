from __future__ import annotations

import pytest


def test_startup_enforcement_errors_collect_provider_and_runtime_failures() -> None:
    from nl2sql_service import main

    errors = main._startup_enforcement_errors(
        {"status": "error", "issues": [{"code": "BASE_URL_REQUIRED"}]},
        {
            "status": "error",
            "mysql_target": {"status": "error", "issues": [{"code": "MYSQL_DRIVER_MISSING"}]},
            "schema_assets": {"status": "error", "issues": [{"code": "SCHEMA_DOCS_MISSING"}]},
        },
    )

    assert len(errors) == 3
    assert any("provider config not ready" in error for error in errors)
    assert any("MySQL target not ready" in error for error in errors)
    assert any("schema assets not ready" in error for error in errors)


def test_enforce_startup_readiness_warn_mode_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import main

    monkeypatch.setattr(main.settings, "startup_enforcement_mode", "warn")
    main._enforce_startup_readiness(
        {"status": "ok", "issues": []},
        {"status": "error", "mysql_target": {"status": "error", "issues": []}, "schema_assets": {"status": "ok", "issues": []}},
    )


def test_enforce_startup_readiness_strict_mode_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nl2sql_service import main

    monkeypatch.setattr(main.settings, "startup_enforcement_mode", "strict")
    with pytest.raises(RuntimeError, match="strict mode"):
        main._enforce_startup_readiness(
            {"status": "ok", "issues": []},
            {
                "status": "error",
                "mysql_target": {"status": "error", "issues": [{"code": "MYSQL_DRIVER_MISSING"}]},
                "schema_assets": {"status": "ok", "issues": []},
            },
        )
