"""
nl2sql_replay_benchmark.py
==========================
Replay stored benchmark cases against /generate-sql and report pass/fail.

Usage
-----
python scripts/nl2sql_replay_benchmark.py --url http://localhost:8080
python scripts/nl2sql_replay_benchmark.py --url http://localhost:8080 --limit 200
python scripts/nl2sql_replay_benchmark.py --url http://localhost:8080 --allow-failures
python scripts/nl2sql_replay_benchmark.py --output reports/replay.json
python scripts/nl2sql_replay_benchmark.py --fail-on-slices join,aggregation
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone


def _get(url: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def _post(url: str, payload: dict, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def _normalize_sql(sql: str) -> str:
    compact = re.sub(r"\s+", " ", sql.strip().rstrip(";"))
    return compact.lower()


def _evaluate_case(case: dict, result: dict) -> tuple[bool, str]:
    expected_status = case.get("expected_status", "ok")
    actual_status = result.get("status")
    if actual_status != expected_status:
        return False, f"status mismatch expected={expected_status} actual={actual_status}"

    gold_sql = case.get("gold_sql")
    if expected_status == "ok" and gold_sql:
        actual_sql = result.get("sql")
        if not actual_sql:
            return False, "expected SQL in ok response but got empty sql"
        if _normalize_sql(actual_sql) != _normalize_sql(gold_sql):
            return False, "gold_sql mismatch"

    return True, "pass"


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay benchmark cases against /generate-sql")
    parser.add_argument("--url", default="http://localhost:8080", help="Base URL of the service")
    parser.add_argument("--limit", type=int, default=100, help="Max benchmark cases to replay")
    parser.add_argument(
        "--active-only",
        action="store_true",
        default=False,
        help="Replay only active benchmark cases",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="top_k value for /generate-sql requests",
    )
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="Exit 0 even when replay failures are detected",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Write structured report to FILE (.json or .csv); file is created or overwritten",
    )
    parser.add_argument(
        "--fail-on-slices",
        metavar="SLICES",
        help=(
            "Comma-separated list of slice names. Exit non-zero only when cases in these "
            "slices fail (other slice failures are ignored unless --allow-failures is not set). "
            "Overrides --allow-failures for the named slices."
        ),
    )
    args = parser.parse_args()

    base = args.url.rstrip("/")
    cases_url = (
        f"{base}/benchmark/cases?limit={args.limit}"
        f"&active_only={'true' if args.active_only else 'false'}"
    )

    try:
        cases_payload = _get(cases_url, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR loading benchmark cases: {exc}", file=sys.stderr)
        sys.exit(1)

    cases = cases_payload.get("results", [])
    if not cases:
        print("No benchmark cases found.")
        return

    # Parse --fail-on-slices into a set for O(1) lookup
    gate_slices: set[str] = set()
    if args.fail_on_slices:
        gate_slices = {s.strip() for s in args.fail_on_slices.split(",") if s.strip()}

    total = 0
    passed = 0
    failed = 0
    by_slice: dict[str, dict[str, int]] = {}
    case_results: list[dict] = []
    run_at = datetime.now(timezone.utc).isoformat()

    print(f"Replaying {len(cases)} benchmark cases against {base}/generate-sql")
    for idx, case in enumerate(cases, start=1):
        payload = {
            "query": case.get("query_text", ""),
            "top_k": args.top_k,
            "request_id": f"replay_{int(time.time())}_{idx}",
        }
        total += 1
        t0 = time.monotonic()
        actual_status: str | None = None

        try:
            result = _post(f"{base}/generate-sql", payload, timeout=args.timeout)
            latency_ms = round((time.monotonic() - t0) * 1000)
            actual_status = result.get("status")
            ok, reason = _evaluate_case(case, result)
        except Exception as exc:  # noqa: BLE001
            latency_ms = round((time.monotonic() - t0) * 1000)
            ok = False
            reason = f"request failure: {exc}"

        slices = case.get("slices") or []
        if not isinstance(slices, list):
            slices = []
        if not slices:
            slices = ["unsliced"]

        for slice_name in slices:
            slice_bucket = by_slice.setdefault(slice_name, {"total": 0, "passed": 0, "failed": 0})
            slice_bucket["total"] += 1
            if ok:
                slice_bucket["passed"] += 1
            else:
                slice_bucket["failed"] += 1

        if ok:
            passed += 1
            verdict = "PASS"
        else:
            failed += 1
            verdict = "FAIL"

        case_results.append(
            {
                "id": case.get("id"),
                "query": case.get("query_text", ""),
                "slices": slices,
                "expected_status": case.get("expected_status"),
                "actual_status": actual_status,
                "pass": ok,
                "reason": reason,
                "latency_ms": latency_ms,
            }
        )

        print(
            f"[{verdict}] id={case.get('id')} expected={case.get('expected_status')} "
            f"query={case.get('query_text')} reason={reason}"
        )

    print("\n=== Replay Summary ===")
    print(f"total={total} passed={passed} failed={failed}")
    pass_rate = (passed / total) * 100 if total else 0.0
    print(f"pass_rate={pass_rate:.2f}%")

    print("\nBy slice:")
    for slice_name in sorted(by_slice):
        stats = by_slice[slice_name]
        slice_pass_rate = (stats["passed"] / stats["total"] * 100) if stats["total"] else 0.0
        print(
            f"- {slice_name}: total={stats['total']} passed={stats['passed']} "
            f"failed={stats['failed']} pass_rate={slice_pass_rate:.2f}%"
        )

    # ---- Export report -------------------------------------------------------
    if args.output:
        _write_report(args.output, run_at, total, passed, failed, pass_rate, by_slice, case_results)
        print(f"\nReport written to: {args.output}")

    # ---- Exit code logic -----------------------------------------------------
    if gate_slices:
        # Only fail if a gated slice has failures
        gated_failures = sum(
            by_slice.get(s, {}).get("failed", 0) for s in gate_slices
        )
        if gated_failures > 0:
            gated_names = ", ".join(sorted(gate_slices))
            print(
                f"\nCI GATE FAILED: {gated_failures} failure(s) in gated slice(s): {gated_names}",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            if failed > 0:
                print(
                    f"\n{failed} failure(s) outside gated slices — not blocking (--fail-on-slices active)"
                )
    elif failed > 0 and not args.allow_failures:
        sys.exit(1)


def _write_report(
    path: str,
    run_at: str,
    total: int,
    passed: int,
    failed: int,
    pass_rate: float,
    by_slice: dict,
    case_results: list[dict],
) -> None:
    import os

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True) if os.path.dirname(path) else None

    if path.endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["id", "query", "slices", "expected_status", "actual_status", "pass", "reason", "latency_ms"],
            )
            writer.writeheader()
            for row in case_results:
                writer.writerow({**row, "slices": "|".join(row["slices"])})
    else:
        report = {
            "run_at": run_at,
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": round(pass_rate, 4),
            "by_slice": {
                name: {
                    **stats,
                    "pass_rate": round(stats["passed"] / stats["total"] * 100, 4) if stats["total"] else 0.0,
                }
                for name, stats in by_slice.items()
            },
            "cases": case_results,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)


if __name__ == "__main__":
    main()
