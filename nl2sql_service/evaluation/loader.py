from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from nl2sql_service.evaluation.models import BenchmarkCase, BenchmarkSuite


def load_benchmark_suites(benchmarks_dir: Path) -> list[BenchmarkSuite]:
    if not benchmarks_dir.exists():
        raise FileNotFoundError(f"Benchmark directory does not exist: {benchmarks_dir}")
    if not benchmarks_dir.is_dir():
        raise NotADirectoryError(f"Benchmark path is not a directory: {benchmarks_dir}")

    suites: list[BenchmarkSuite] = []
    for path in sorted(benchmarks_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        suite = BenchmarkSuite.model_validate(payload)
        suite.metadata.setdefault("source_file", path.name)
        suites.append(suite)
    return suites


def iter_benchmark_cases(suites: Iterable[BenchmarkSuite]) -> Iterable[tuple[BenchmarkSuite, BenchmarkCase]]:
    for suite in suites:
        for case in suite.cases:
            yield suite, case


def build_db_sync_payload(suite: BenchmarkSuite, case: BenchmarkCase) -> dict[str, object]:
    metadata = {
        "suite": suite.model_dump(mode="json"),
        "case": case.model_dump(mode="json"),
    }
    likely_failure_types = [
        failure_type.value
        for failure_type in case.failure_classification_hints.likely_failure_types
    ]
    slices = [suite.suite_id, f"level-{suite.level}"]
    case_slices = case.metadata.get("slices", [])
    if isinstance(case_slices, str):
        case_slices = [case_slices]
    if not isinstance(case_slices, list):
        case_slices = []
    for value in case_slices:
        slice_name = str(value).strip()
        if slice_name and slice_name not in slices:
            slices.append(slice_name)
    return {
        "query": case.query,
        "gold_sql": case.metadata.get("gold_sql"),
        "expected_status": case.expected_criteria.status,
        "slices": slices,
        "error_label": likely_failure_types[0] if likely_failure_types else None,
        "source": case.metadata.get("source", "evaluation-cli"),
        "metadata": metadata,
    }
