from nl2sql_service.db.db import _normalize_failure_log_row


def test_normalize_failure_log_row_parses_legacy_stringified_json() -> None:
    row = {
        "id": 1,
        "request_id": "req-1",
        "endpoint": "/ask",
        "query_text": "show me the 5 most recent contacts",
        "warning_codes": '["REQUEST_TIMEOUT"]',
        "error_source": "timeout",
        "sql_preview": "",
        "tables_attempted": '["contact"]',
        "latency_ms": 1234,
        "suggest_teach": (
            '{"content":"Fix contacts query","sql_preview":"","source_query":"show me the 5 most recent contacts",'
            '"warning_codes":["REQUEST_TIMEOUT"],"tables_affected":[],"instruction_type":"term_mapping"}'
        ),
        "failure_details": '{"root_cause":"REQUEST_TIMEOUT"}',
        "created_at": "2026-06-02T10:55:55.417393",
    }

    normalized = _normalize_failure_log_row(row)

    assert normalized["warning_codes"] == ["REQUEST_TIMEOUT"]
    assert normalized["tables_attempted"] == ["contact"]
    assert normalized["suggest_teach"]["instruction_type"] == "term_mapping"
    assert normalized["failure_details"]["root_cause"] == "REQUEST_TIMEOUT"
