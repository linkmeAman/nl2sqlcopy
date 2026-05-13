"""
Run a full-route smoke matrix for the NL2SQL service.

Usage:
  ./.venv/bin/python scripts/nl2sql_smoke_test.py --url http://localhost:8080
  ./.venv/bin/python scripts/nl2sql_smoke_test.py --url http://localhost:8080 --output reports/smoke.json
  ./.venv/bin/python scripts/nl2sql_smoke_test.py --url http://localhost:8080 --output reports/smoke.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class RouteResult:
    name: str
    method: str
    path: str
    status_code: int
    passed: bool
    latency_ms: int
    note: str


def _request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    timeout: int,
) -> tuple[int, Any, int]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        code = exc.code

    latency_ms = max(0, int((time.monotonic() - t0) * 1000))
    try:
        body = json.loads(raw)
    except Exception:  # noqa: BLE001
        body = raw
    return code, body, latency_ms


def _expect_status(code: int, expected: set[int]) -> tuple[bool, str]:
    ok = code in expected
    return ok, "ok" if ok else f"unexpected status {code}"


def _expect_key(body: Any, key: str) -> tuple[bool, str]:
    if not isinstance(body, dict):
        return False, "response is not a JSON object"
    if key not in body:
        return False, f"missing key '{key}'"
    return True, "ok"


def _expect_ndjson_final(body: Any) -> tuple[bool, str]:
    if not isinstance(body, str):
        return False, "stream response was not text"
    lines = [line for line in body.splitlines() if line.strip()]
    if not lines:
        return False, "stream had no events"
    try:
        final = json.loads(lines[-1])
    except Exception:  # noqa: BLE001
        return False, "last line was not JSON"
    if final.get("event") != "final":
        return False, "missing final event"
    if not isinstance(final.get("response"), dict):
        return False, "final event missing response object"
    return True, "ok"


def _request_cli(args: list[str], expected_text: str, timeout: int) -> tuple[int, str, bool, int, str]:
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    latency_ms = max(0, int((time.monotonic() - t0) * 1000))
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        return proc.returncode, output, False, latency_ms, f"exit {proc.returncode}"
    if expected_text not in output:
        return proc.returncode, output, False, latency_ms, f"missing text '{expected_text}'"
    return proc.returncode, output, True, latency_ms, "ok"


def _print_matrix(results: list[RouteResult]) -> None:
    print("\n=== Smoke Matrix ===")
    header = f"{'Route':42} {'Method':6} {'Status':6} {'Pass':4} {'Latency(ms)':11} Note"
    print(header)
    print("-" * len(header))
    for item in results:
        route = item.path[:42]
        status = "PASS" if item.passed else "FAIL"
        print(f"{route:42} {item.method:6} {item.status_code:<6} {status:4} {item.latency_ms:<11} {item.note}")


def _write_report(path: str, run_at: str, base_url: str, results: list[RouteResult]) -> None:
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed

    if report_path.suffix.lower() == ".csv":
        with report_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["name", "method", "path", "status_code", "passed", "latency_ms", "note"],
            )
            writer.writeheader()
            for row in results:
                writer.writerow(
                    {
                        "name": row.name,
                        "method": row.method,
                        "path": row.path,
                        "status_code": row.status_code,
                        "passed": row.passed,
                        "latency_ms": row.latency_ms,
                        "note": row.note,
                    }
                )
        return

    payload = {
        "run_at": run_at,
        "base_url": base_url,
        "summary": {"total": total, "passed": passed, "failed": failed},
        "results": [
            {
                "name": row.name,
                "method": row.method,
                "path": row.path,
                "status_code": row.status_code,
                "passed": row.passed,
                "latency_ms": row.latency_ms,
                "note": row.note,
            }
            for row in results
        ],
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run full-route smoke matrix for NL2SQL")
    parser.add_argument("--url", default="http://localhost:8080", help="Service base URL")
    parser.add_argument("--timeout", type=int, default=180, help="HTTP timeout in seconds")
    parser.add_argument("--output", help="Optional JSON or CSV output file")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    now = datetime.now(timezone.utc).isoformat()

    token = str(int(time.time()))
    teach_content = f"smoke advisor mapping {token}"
    delete_instruction_id: int | None = None

    checks: list[tuple[str, str, str, dict[str, Any] | None, set[int], str]] = [
        ("health", "GET", "/health", None, {200}, "key:status"),
        ("help_index", "GET", "/help", None, {200}, "text:NL2SQL Route Help"),
        ("help_generation", "GET", "/help/generation", None, {200}, "text:Generation Routes"),
        ("help_ask", "GET", "/help/generation/ask", None, {200}, "text:Ask Question"),
        ("telemetry_recent", "GET", "/telemetry/recent?limit=5", None, {200}, "key:results"),
        ("telemetry_summary", "GET", "/telemetry/summary?since_minutes=60", None, {200}, "key:total_requests"),
        ("benchmark_create", "POST", "/benchmark/cases", {
            "query": f"smoke benchmark query {token}",
            "expected_status": "ok",
            "slices": ["smoke"],
            "source": "smoke-script",
        }, {200}, "key:id"),
        ("benchmark_list", "GET", "/benchmark/cases?limit=5&active_only=true", None, {200}, "key:results"),
        ("ingest_text", "POST", "/ingest", {
            "type": "text",
            "source": f"smoke_{token}",
            "text": "smoke test invoice payment counselor context",
        }, {200}, "key:inserted"),
        ("query", "POST", "/query", {"query": "invoice payment status", "top_k": 3}, {200}, "key:results"),
        ("ingest_groups", "POST", "/ingest/groups", {"group_names": ["inquiry_lifecycle"]}, {200}, "key:inserted"),
        ("ingest_groups_status", "GET", "/ingest/groups/status", None, {200}, "key:groups"),
        ("ingest_knowledge", "POST", "/ingest/knowledge", {
            "include_column_catalog": False,
            "include_sql_examples": False,
            "include_relations": False,
            "include_graph": False,
            "include_view_registry": False,
            "include_onboarding_rules": False,
        }, {200}, "key:source"),
        ("ingest_patterns", "POST", "/ingest/patterns", {}, {200}, "key:source"),
        ("ingest_instructions", "POST", "/ingest/instructions", {}, {200}, "key:source"),
        ("query_groups", "POST", "/query/groups", {"query": "show unpaid invoices by counselor", "top_k": 3}, {200}, "key:context"),
        ("teach", "POST", "/teach", {
            "instruction_type": "term_mapping",
            "content": teach_content,
            "tables_affected": ["employee"],
            "source_query": "smoke script",
        }, {200}, "key:learning_status"),
        ("instructions_list", "GET", "/instructions?active_only=true", None, {200}, "json:list"),
        ("generate_sql", "POST", "/generate-sql", {"query": "show me the 5 most recent inquiries", "top_k": 3}, {200}, "key:status"),
        ("ask", "POST", "/ask", {"query": "show me the 5 most recent inquiries", "top_k": 3}, {200}, "key:status"),
        ("ask_stream", "POST", "/ask/stream", {"query": "show me the 5 most recent inquiries", "top_k": 3}, {200}, "ndjson:final"),
        ("patterns_feedback", "POST", "/patterns/feedback", {"pattern_id": 1, "helpful": True}, {200, 404}, "key:action_or_detail"),
    ]
    cli_checks: list[tuple[str, list[str], str]] = [
        ("help_tui_plain", ["-m", "nl2sql_service.help_tui", "--plain"], "NL2SQL Terminal Help"),
        ("help_tui_generation", ["-m", "nl2sql_service.help_tui", "--module", "generation", "--plain"], "Generation"),
        ("help_tui_ask", ["-m", "nl2sql_service.help_tui", "--route", "generation/ask", "--plain"], "Ask Question"),
    ]

    results: list[RouteResult] = []

    for name, method, path, payload, expected_statuses, validator in checks:
        code, body, latency_ms = _request_json(method, f"{base}{path}", payload, timeout=args.timeout)

        passed, note = _expect_status(code, expected_statuses)
        if passed:
            if validator.startswith("key:"):
                if validator == "key:action_or_detail":
                    if isinstance(body, dict) and ("action" in body or "detail" in body):
                        passed, note = True, "ok"
                    else:
                        passed, note = False, "missing action/detail"
                else:
                    key = validator.split(":", 1)[1]
                    passed, note = _expect_key(body, key)
            elif validator == "json:list":
                passed = isinstance(body, list)
                note = "ok" if passed else "response is not a JSON list"
            elif validator == "ndjson:final":
                passed, note = _expect_ndjson_final(body)
            elif validator.startswith("text:"):
                expected_text = validator.split(":", 1)[1]
                passed = isinstance(body, str) and expected_text in body
                note = "ok" if passed else f"missing text '{expected_text}'"

        if name == "teach" and isinstance(body, dict):
            if isinstance(body.get("instruction_id"), int):
                delete_instruction_id = int(body["instruction_id"])

        results.append(
            RouteResult(
                name=name,
                method=method,
                path=path,
                status_code=code,
                passed=passed,
                latency_ms=latency_ms,
                note=note,
            )
        )

    for name, args_list, expected_text in cli_checks:
        code, _output, passed, latency_ms, note = _request_cli(args_list, expected_text, timeout=args.timeout)
        results.append(
            RouteResult(
                name=name,
                method="CLI",
                path="python " + " ".join(args_list),
                status_code=code,
                passed=passed,
                latency_ms=latency_ms,
                note=note,
            )
        )

    if delete_instruction_id is not None:
        code, body, latency_ms = _request_json(
            "DELETE",
            f"{base}/instructions/{delete_instruction_id}",
            None,
            timeout=args.timeout,
        )
        passed, note = _expect_status(code, {200})
        if passed:
            passed, note = _expect_key(body, "deactivated")
        results.append(
            RouteResult(
                name="instructions_delete",
                method="DELETE",
                path=f"/instructions/{delete_instruction_id}",
                status_code=code,
                passed=passed,
                latency_ms=latency_ms,
                note=note,
            )
        )

    _print_matrix(results)

    total = len(results)
    passed_count = sum(1 for result in results if result.passed)
    failed_count = total - passed_count
    print("\n=== Summary ===")
    print(f"run_at={now}")
    print(f"base_url={base}")
    print(f"total={total} passed={passed_count} failed={failed_count}")

    if args.output:
        _write_report(args.output, now, base, results)
        print(f"report={args.output}")

    if failed_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
