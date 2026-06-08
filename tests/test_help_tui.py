from __future__ import annotations

import subprocess
import sys

from nl2sql_service import help_docs, help_tui


def _run_help_tui(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "nl2sql_service.help_tui", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_help_tui_module_imports() -> None:
    assert callable(help_tui.main)


def test_help_tui_plain_lists_routes() -> None:
    result = _run_help_tui("--plain")
    assert result.returncode == 0, result.stderr
    assert "NL2SQL Terminal Help" in result.stdout
    assert "/ask" in result.stdout
    assert "/generate-sql" in result.stdout


def test_help_tui_module_generation_plain() -> None:
    result = _run_help_tui("--module", "generation", "--plain")
    assert result.returncode == 0, result.stderr
    assert "Generation" in result.stdout
    assert "/ask" in result.stdout
    assert "/generate-sql" in result.stdout
    assert "/ingest/knowledge" not in result.stdout


def test_help_tui_route_generation_ask_plain() -> None:
    result = _run_help_tui("--route", "generation/ask", "--plain")
    assert result.returncode == 0, result.stderr
    assert "Ask Question" in result.stdout
    assert "Route: POST /ask" in result.stdout
    assert "Request Body" in result.stdout
    assert "Response" in result.stdout
    assert "Error Responses" in result.stdout
    assert "Authentication" in result.stdout
    assert "curl -s -X POST" in result.stdout
    assert "Related Routes" in result.stdout


def test_help_tui_search_sql_plain() -> None:
    result = _run_help_tui("--search", "sql", "--plain")
    assert result.returncode == 0, result.stderr
    assert "search: sql" in result.stdout
    assert "/generate-sql" in result.stdout
    assert "Generate SQL" in result.stdout


def test_help_tui_data_source_matches_help_registry(app) -> None:
    expected = help_docs.build_help_index(app.openapi())
    actual = help_tui.load_help_index()

    assert set(actual.by_key) == set(expected.by_key)
    assert [endpoint.key for endpoint in actual.endpoints] == [endpoint.key for endpoint in expected.endpoints]


def test_help_tui_is_db_free_when_pool_missing(app) -> None:
    original = getattr(app.state, "pool", None)
    app.state.pool = None
    try:
        index = help_tui.load_help_index()
        output = help_tui.render_route_list(index, module="generation")
    finally:
        app.state.pool = original

    assert "/ask" in output
    assert "/generate-sql" in output
