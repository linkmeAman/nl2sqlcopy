from __future__ import annotations

from nl2sql_service.evaluation.models import (
    BenchmarkCase,
    BenchmarkExpectedCriteria,
    BenchmarkSuite,
    BenchmarkSqlCharacteristics,
    EvaluationEndpoint,
)
from nl2sql_service.evaluation.runner import _build_run_specs


def test_build_run_specs_expands_top_k_and_repeat() -> None:
    suite = BenchmarkSuite(
        suite_id="level5_stress",
        level=5,
        title="Level 5 - Stress Test",
        cases=[
            BenchmarkCase(
                id="case-1",
                query="show me the latest payments",
                expected_criteria=BenchmarkExpectedCriteria(status="ok"),
                expected_sql_characteristics=BenchmarkSqlCharacteristics(),
                endpoint=EvaluationEndpoint.ASK_STREAM,
                top_k_values=[10, 3, 5],
                repeat=2,
            )
        ],
    )

    specs = _build_run_specs(
        [suite],
        default_endpoint=EvaluationEndpoint.ASK,
        default_top_k=7,
        default_repeat=1,
    )

    assert [spec.top_k for spec in specs] == [3, 3, 5, 5, 10, 10]
    assert all(spec.endpoint == EvaluationEndpoint.ASK_STREAM for spec in specs)
    assert len({spec.request_id for spec in specs}) == 6
