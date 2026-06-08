"""
Production-grade NL2SQL evaluation CLI.

Usage:
  ./.venv/bin/python scripts/nl2sql_evaluate.py --url http://localhost:8080
  ./.venv/bin/python scripts/nl2sql_evaluate.py --url http://localhost:8080 --endpoint ask-stream --parallel 4
  ./.venv/bin/python scripts/nl2sql_evaluate.py --url http://localhost:8080 --sync-db
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nl2sql_service.evaluation.models import EvaluationConfig, EvaluationEndpoint
from nl2sql_service.evaluation.runner import EvaluationRunner


def _parse_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for value in values:
        if ":" not in value:
            raise ValueError(f"Invalid header format {value!r}; expected 'Name: Value'")
        name, raw = value.split(":", 1)
        name = name.strip()
        raw = raw.strip()
        if not name:
            raise ValueError(f"Invalid header name in {value!r}")
        headers[name] = raw
    return headers


def _print_summary(summary) -> None:
    print(
        f"Evaluated {summary.total_tests} runs: "
        f"passed={summary.passed} failed={summary.failed} "
        f"pass_rate={summary.pass_rate:.2%} "
        f"avg_latency_ms={summary.avg_latency_ms:.1f} "
        f"p95_latency_ms={summary.p95_latency_ms:.1f}",
        file=sys.stderr,
    )
    print(
        "Failure breakdown: "
        + (", ".join(f"{key}={value}" for key, value in summary.failure_breakdown.items()) or "none"),
        file=sys.stderr,
    )
    print(f"Summary JSON: {summary.output_files.get('summary_json')}", file=sys.stderr)
    print(f"Failure JSONL: {summary.output_files.get('failures_jsonl')}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the NL2SQL evaluation framework")
    parser.add_argument("--url", default="http://localhost:8080", help="NL2SQL service base URL")
    parser.add_argument(
        "--benchmarks",
        default="benchmarks",
        help="Directory containing level1_basic.json ... level5_stress.json",
    )
    parser.add_argument(
        "--output-dir",
        default="reports/evaluation",
        help="Directory for evaluation_failures.jsonl and evaluation_summary.json",
    )
    parser.add_argument(
        "--endpoint",
        choices=[item.value for item in EvaluationEndpoint],
        default=EvaluationEndpoint.ASK.value,
        help="Default request endpoint for benchmark execution",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Default top_k for cases that do not override it")
    parser.add_argument("--parallel", type=int, default=1, help="Number of concurrent benchmark runs")
    parser.add_argument("--repeat", type=int, default=1, help="Minimum repeat count applied to every benchmark case")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--trace-retry-limit",
        type=int,
        default=6,
        help="How many times to poll /telemetry/trace/{request_id} before giving up",
    )
    parser.add_argument(
        "--trace-retry-delay",
        type=float,
        default=0.25,
        help="Initial delay in seconds between trace polling attempts",
    )
    parser.add_argument(
        "--fail-limit",
        type=int,
        default=500,
        help="How many recent failure records to inspect when enriching a run",
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Fail fast if /health, /health/config, or /health/runtime is not ready",
    )
    parser.add_argument(
        "--sync-db",
        action="store_true",
        help="Persist local benchmark cases into /benchmark/cases before running the evaluation",
    )
    parser.add_argument("--bearer-token", help="Bearer token for authenticated deployments")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help="Additional header to send with each request; can be repeated",
    )
    args = parser.parse_args(argv)

    try:
        extra_headers = _parse_headers(args.header)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    config = EvaluationConfig(
        service_url=args.url,
        benchmarks_dir=Path(args.benchmarks),
        output_dir=Path(args.output_dir),
        endpoint=EvaluationEndpoint(args.endpoint),
        top_k=args.top_k,
        parallel=args.parallel,
        timeout_seconds=args.timeout,
        trace_retry_limit=args.trace_retry_limit,
        trace_retry_delay_seconds=args.trace_retry_delay,
        require_ready=args.require_ready,
        sync_db=args.sync_db,
        repeat=args.repeat,
        bearer_token=args.bearer_token,
        extra_headers=extra_headers,
        fail_limit=args.fail_limit,
    )

    summary = asyncio.run(EvaluationRunner(config).run())
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
