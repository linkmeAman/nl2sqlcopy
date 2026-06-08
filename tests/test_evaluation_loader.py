from __future__ import annotations

import json

from nl2sql_service.evaluation.loader import build_db_sync_payload, load_benchmark_suites
from nl2sql_service.evaluation.models import (
    BenchmarkCase,
    BenchmarkExpectedCriteria,
    BenchmarkSuite,
    BenchmarkSqlCharacteristics,
    FailureHints,
    FailureType,
)


def test_load_benchmark_suites_sets_source_file(tmp_path) -> None:
    suite_payload = {
        "suite_id": "level1_basic",
        "level": 1,
        "title": "Level 1 - Basic",
        "description": "Basic retrieval checks.",
        "cases": [
            {
                "id": "case-1",
                "query": "show me the latest payments",
                "expected_criteria": {"status": "ok"},
                "expected_tables": ["payment"],
                "expected_keywords": ["payment"],
                "expected_sql_characteristics": {"must_include": ["FROM payment"]},
                "failure_classification_hints": {"likely_failure_types": ["RETRIEVAL_FAILURE"]},
            }
        ],
    }
    (tmp_path / "level1_basic.json").write_text(json.dumps(suite_payload), encoding="utf-8")

    suites = load_benchmark_suites(tmp_path)

    assert len(suites) == 1
    assert suites[0].suite_id == "level1_basic"
    assert suites[0].metadata["source_file"] == "level1_basic.json"


def test_build_db_sync_payload_coerces_metadata_slices() -> None:
    suite = BenchmarkSuite(
        suite_id="level1_basic",
        level=1,
        title="Level 1 - Basic",
        cases=[],
    )
    case = BenchmarkCase(
        id="case-1",
        query="show me the latest payments",
        expected_criteria=BenchmarkExpectedCriteria(status="ok"),
        expected_tables=["payment"],
        expected_keywords=["payment"],
        expected_sql_characteristics=BenchmarkSqlCharacteristics(must_include=["FROM payment"]),
        failure_classification_hints=FailureHints(likely_failure_types=[FailureType.RETRIEVAL_FAILURE]),
        metadata={"slices": "billing", "source": "unit-test", "gold_sql": "SELECT id FROM payment"},
    )

    payload = build_db_sync_payload(suite, case)

    assert payload["expected_status"] == "ok"
    assert payload["error_label"] == "RETRIEVAL_FAILURE"
    assert payload["source"] == "unit-test"
    assert payload["slices"] == ["level1_basic", "level-1", "billing"]
