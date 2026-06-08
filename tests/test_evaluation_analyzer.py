from __future__ import annotations

from nl2sql_service.evaluation.analyzer import assess_run, build_failure_record
from nl2sql_service.evaluation.models import (
    BenchmarkCase,
    BenchmarkExpectedCriteria,
    BenchmarkSqlCharacteristics,
    EvaluationEndpoint,
    FailureType,
    RunResult,
    RunSpec,
)


def _build_spec(case: BenchmarkCase, request_id: str = "req-1") -> RunSpec:
    return RunSpec(
        suite_id="suite-1",
        suite_level=1,
        suite_title="Suite 1",
        case=case,
        endpoint=EvaluationEndpoint.ASK,
        top_k=5,
        repeat_index=0,
        variant_index=0,
        request_id=request_id,
        trace_id="trace-1",
    )


def test_assess_run_classifies_retrieval_failure_when_expected_tables_are_missing() -> None:
    case = BenchmarkCase(
        id="case-1",
        query="show me the latest payments",
        expected_criteria=BenchmarkExpectedCriteria(status="ok"),
        expected_tables=["payment"],
        expected_keywords=["status"],
        expected_sql_characteristics=BenchmarkSqlCharacteristics(
            must_include=["FROM payment", "LIMIT"],
            must_use_tables=["payment"],
            forbid_select_star=True,
            requires_limit=True,
        ),
    )
    run = RunResult(
        spec=_build_spec(case),
        timestamp="2026-06-08T00:00:00Z",
        latency_ms=123,
        http_status_code=200,
        response_body={
            "status": "ok",
            "answer": "Recent payment rows",
            "sql": "SELECT id FROM payment LIMIT 5",
            "tables_used": ["payment"],
            "matched_groups": ["billing"],
            "warnings": [],
        },
        response_status="ok",
        actual_answer="Recent payment rows",
        generated_sql="SELECT id FROM payment LIMIT 5",
    )

    assessment = assess_run(case, run)

    assert assessment.passed is False
    assert assessment.failure_type == FailureType.RETRIEVAL_FAILURE
    assert any("missing expected keyword" in reason for reason in assessment.reasons)
    assert "http_status_code=200" in assessment.failure_signals


def test_assess_run_classifies_provider_failure_on_http_error() -> None:
    case = BenchmarkCase(
        id="case-2",
        query="show me the latest payments",
        expected_criteria=BenchmarkExpectedCriteria(status="ok"),
        expected_sql_characteristics=BenchmarkSqlCharacteristics(),
    )
    run = RunResult(
        spec=_build_spec(case, request_id="req-2"),
        timestamp="2026-06-08T00:00:00Z",
        latency_ms=87,
        http_status_code=503,
        response_body={"detail": "Service Unavailable"},
        request_error=None,
    )

    assessment = assess_run(case, run)

    assert assessment.passed is False
    assert assessment.failure_type == FailureType.PROVIDER_FAILURE
    assert "http_status_code=503" in assessment.failure_signals


def test_build_failure_record_includes_transport_and_failure_log_context() -> None:
    case = BenchmarkCase(
        id="case-3",
        query="show me the latest payments",
        expected_criteria=BenchmarkExpectedCriteria(status="ok"),
        expected_sql_characteristics=BenchmarkSqlCharacteristics(),
    )
    run = RunResult(
        spec=_build_spec(case, request_id="req-3"),
        timestamp="2026-06-08T00:00:00Z",
        latency_ms=42,
        http_status_code=503,
        response_body={"detail": "Service Unavailable"},
        request_error="connection reset by peer",
        failure_log_entry={"request_id": "req-3", "failure_type": "PROVIDER_FAILURE"},
    )

    record = build_failure_record(case, run)

    assert record["http_status_code"] == 503
    assert record["request_error"] == "connection reset by peer"
    assert record["failure_log_entry"]["failure_type"] == "PROVIDER_FAILURE"
