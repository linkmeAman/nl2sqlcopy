from __future__ import annotations

import json

from nl2sql_service.evaluation.logger import JsonlFailureLogger


def test_jsonl_failure_logger_writes_one_record_per_line(tmp_path) -> None:
    path = tmp_path / "failures.jsonl"

    with JsonlFailureLogger(path) as logger:
        logger.write({"test_id": "case-1", "failure_type": "RETRIEVAL_FAILURE"})
        logger.write({"test_id": "case-2", "failure_type": "PROVIDER_FAILURE"})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["test_id"] == "case-1"
    assert json.loads(lines[1])["failure_type"] == "PROVIDER_FAILURE"
