from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nl2sql_service.evaluation.analyzer import assess_run, build_failure_record
from nl2sql_service.evaluation.client import Nl2SqlEvaluationClient, ClientResponse
from nl2sql_service.evaluation.loader import (
    build_db_sync_payload,
    iter_benchmark_cases,
    load_benchmark_suites,
)
from nl2sql_service.evaluation.logger import JsonlFailureLogger
from nl2sql_service.evaluation.models import (
    BenchmarkCase,
    BenchmarkSuite,
    EvaluationConfig,
    EvaluationEndpoint,
    EvaluationSummary,
    FailureType,
    RunResult,
    RunSpec,
)


def _normalize_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "case"


def _unique_sorted(values: list[int]) -> list[int]:
    return sorted({int(value) for value in values if int(value) > 0})


def _percentile(values: list[int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[int(rank)])
    lower_value = ordered[lower]
    upper_value = ordered[upper]
    return float(lower_value + (upper_value - lower_value) * (rank - lower))


def _response_body_to_dict(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        return body
    if body is None:
        return {}
    return {"raw": body}


def _build_request_ids(
    *,
    suite: BenchmarkSuite,
    case: BenchmarkCase,
    top_k: int,
    repeat_index: int,
    variant_index: int,
) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    request_id = (
        f"eval-{_normalize_slug(suite.suite_id)}-"
        f"{_normalize_slug(case.id)}-k{top_k}-r{repeat_index + 1}-v{variant_index + 1}-{suffix}"
    )
    trace_id = uuid.uuid4().hex
    return request_id, trace_id


def _build_run_specs(
    suites: list[BenchmarkSuite],
    *,
    default_endpoint: EvaluationEndpoint,
    default_top_k: int,
    default_repeat: int,
) -> list[RunSpec]:
    specs: list[RunSpec] = []
    for suite in suites:
        for case in suite.cases:
            endpoint = case.endpoint or default_endpoint
            top_k_values = _unique_sorted(case.top_k_values)
            if not top_k_values:
                top_k_values = [int(case.top_k or default_top_k)]
            repeat_count = max(1, int(case.repeat or 1), int(default_repeat or 1))
            variant_index = 0
            for top_k in top_k_values:
                for repeat_index in range(repeat_count):
                    request_id, trace_id = _build_request_ids(
                        suite=suite,
                        case=case,
                        top_k=top_k,
                        repeat_index=repeat_index,
                        variant_index=variant_index,
                    )
                    specs.append(
                        RunSpec(
                            suite_id=suite.suite_id,
                            suite_level=suite.level,
                            suite_title=suite.title,
                            case=case,
                            endpoint=endpoint,
                            top_k=top_k,
                            repeat_index=repeat_index,
                            variant_index=variant_index,
                            request_id=request_id,
                            trace_id=trace_id,
                        )
                    )
                    variant_index += 1
    return specs


def _extract_trace_id(trace_payload: dict[str, Any], response_body: dict[str, Any]) -> str | None:
    if response_body.get("trace_id"):
        return str(response_body["trace_id"])
    results = trace_payload.get("results") or []
    for event in results:
        if isinstance(event, dict) and event.get("trace_id"):
            return str(event["trace_id"])
    return None


def _collect_react_trace_steps(response_body: dict[str, Any]) -> list[dict[str, Any]]:
    react_trace = response_body.get("react_trace")
    if not isinstance(react_trace, dict):
        return []
    steps = react_trace.get("steps") or []
    if not isinstance(steps, list):
        return []
    collected: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        collected.append(
            {
                "iteration": step.get("iteration"),
                "action": step.get("action"),
                "action_input": step.get("action_input"),
                "observation": step.get("observation"),
                "duration_ms": step.get("duration_ms"),
            }
        )
    return collected


def _collect_summary_buckets(results: list[RunResult]) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    by_level: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
    by_suite: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
    for result in results:
        level_key = f"level-{result.spec.suite_level}"
        suite_key = result.spec.suite_id
        level_bucket = by_level[level_key]
        suite_bucket = by_suite[suite_key]
        level_bucket["total"] += 1
        suite_bucket["total"] += 1
        if result.assessment and result.assessment.passed:
            level_bucket["passed"] += 1
            suite_bucket["passed"] += 1
        else:
            level_bucket["failed"] += 1
            suite_bucket["failed"] += 1
    return dict(by_level), dict(by_suite)


def _failure_breakdown(results: list[RunResult]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for result in results:
        assessment = result.assessment
        if not assessment or assessment.passed or assessment.failure_type is None:
            continue
        counts[assessment.failure_type.value] += 1
    return dict(sorted(counts.items(), key=lambda item: item[0]))


def _failure_rate(results: list[RunResult], failure_types: set[FailureType]) -> float:
    total = len(results)
    if not total:
        return 0.0
    count = 0
    for result in results:
        assessment = result.assessment
        if assessment and assessment.failure_type in failure_types:
            count += 1
    return count / total


def _serialize_failure_record(record: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(record, default=str))


class EvaluationRunner:
    def __init__(self, config: EvaluationConfig) -> None:
        self.config = config

    async def run(self) -> EvaluationSummary:
        output_dir = self.config.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        failures_path = output_dir / "evaluation_failures.jsonl"
        summary_path = output_dir / "evaluation_summary.json"

        suites = load_benchmark_suites(self.config.benchmarks_dir)
        if not suites:
            raise RuntimeError(f"No benchmark suites found in {self.config.benchmarks_dir}")

        client = Nl2SqlEvaluationClient(
            service_url=self.config.service_url,
            timeout_seconds=self.config.timeout_seconds,
            bearer_token=self.config.bearer_token,
            extra_headers=self.config.extra_headers,
        )
        try:
            if self.config.require_ready:
                await client.ensure_ready()

            if self.config.sync_db:
                for suite, case in iter_benchmark_cases(suites):
                    await client.sync_benchmark_case(build_db_sync_payload(suite, case))

            specs = _build_run_specs(
                suites,
                default_endpoint=self.config.endpoint,
                default_top_k=self.config.top_k,
                default_repeat=self.config.repeat,
            )
            results = await self._run_specs(client, specs)
        finally:
            await client.aclose()

        failures = [result for result in results if not result.assessment or not result.assessment.passed]
        with JsonlFailureLogger(failures_path) as logger:
            for result in failures:
                if result.assessment is None:
                    continue
                logger.write(_serialize_failure_record(build_failure_record(result.spec.case, result)))

        summary = self._build_summary(results)
        summary.output_files = {
            "failures_jsonl": str(failures_path),
            "summary_json": str(summary_path),
        }
        summary_path.write_text(json.dumps(summary.to_dict(), indent=2, default=str), encoding="utf-8")
        return summary

    async def _run_specs(self, client: Nl2SqlEvaluationClient, specs: list[RunSpec]) -> list[RunResult]:
        semaphore = asyncio.Semaphore(max(1, int(self.config.parallel)))
        results: list[RunResult | None] = [None] * len(specs)

        async def run_one(index: int, spec: RunSpec) -> None:
            async with semaphore:
                result = await self._run_spec(client, spec)
                results[index] = result

        await asyncio.gather(*(run_one(index, spec) for index, spec in enumerate(specs)))
        return [result for result in results if result is not None]

    async def _run_spec(self, client: Nl2SqlEvaluationClient, spec: RunSpec) -> RunResult:
        started = datetime.now(timezone.utc)
        request_started = time.monotonic()
        request_error: str | None = None
        client_response: ClientResponse | None = None
        response_body: dict[str, Any] = {}
        trace_payload: dict[str, Any] = {"request_id": spec.request_id, "results": [], "total": 0}
        failure_log_entry: dict[str, Any] | None = None

        try:
            client_response = await client.ask(spec)
            response_body = _response_body_to_dict(client_response.body)
        except Exception as exc:  # noqa: BLE001
            request_error = str(exc)
            client_response = None

        latency_ms = (
            client_response.latency_ms
            if client_response is not None
            else int((time.monotonic() - request_started) * 1000)
        )
        response_status = str(response_body.get("status") or "").strip() or None
        run = RunResult(
            spec=spec,
            timestamp=started.isoformat(),
            latency_ms=latency_ms,
            http_status_code=client_response.status_code if client_response is not None else None,
            response_body=response_body,
            response_status=response_status,
            actual_answer=response_body.get("answer"),
            generated_sql=response_body.get("sql"),
            warnings=list(response_body.get("warnings") or []),
            row_count=response_body.get("row_count"),
            columns=list(response_body.get("columns") or []),
            tables_used=list(response_body.get("tables_used") or []),
            matched_groups=list(response_body.get("matched_groups") or []),
            attempt_count=response_body.get("attempt_count"),
            cache_hit=bool(response_body.get("cache_hit", False)),
            cache_source=str(response_body.get("cache_source") or "") or None,
            trace_id=str(response_body.get("trace_id") or "") or None,
            stream_events=list(client_response.events or []) if client_response else [],
            request_error=request_error,
        )

        try:
            trace_payload = await client.get_trace_with_retry(
                spec.request_id,
                limit=1000,
                retries=self.config.trace_retry_limit,
                delay_seconds=self.config.trace_retry_delay_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            trace_payload = {
                "request_id": spec.request_id,
                "results": [],
                "total": 0,
                "error": str(exc),
            }
        run.trace_events = list(trace_payload.get("results") or [])
        run.trace_id = _extract_trace_id(trace_payload, response_body) or run.trace_id
        try:
            failure_log_entry = await client.get_failure_for_request(
                spec.request_id,
                endpoint=f"/{spec.endpoint.value.replace('-', '/')}",
                limit=self.config.fail_limit,
            )
        except Exception as exc:  # noqa: BLE001
            failure_log_entry = {
                "request_id": spec.request_id,
                "error": str(exc),
            }
        run.failure_log_entry = failure_log_entry
        assessment = assess_run(spec.case, run)
        run.assessment = assessment
        return run

    def _build_summary(self, results: list[RunResult]) -> EvaluationSummary:
        total = len(results)
        passed = sum(1 for result in results if result.assessment and result.assessment.passed)
        failed = total - passed
        latencies = [result.latency_ms for result in results]
        cache_hits = sum(1 for result in results if result.assessment and result.assessment.cache_hit)
        retrieval_failure_rate = _failure_rate(
            results,
            {
                FailureType.RETRIEVAL_FAILURE,
                FailureType.CHUNKING_FAILURE,
                FailureType.RERANKING_FAILURE,
                FailureType.SCHEMA_RETRIEVAL_FAILURE,
            },
        )
        sql_failure_rate = _failure_rate(
            results,
            {FailureType.SQL_GENERATION_FAILURE, FailureType.SQL_VALIDATION_FAILURE},
        )
        provider_failure_rate = _failure_rate(results, {FailureType.PROVIDER_FAILURE})
        by_level, by_suite = _collect_summary_buckets(results)
        failure_breakdown = _failure_breakdown(results)
        return EvaluationSummary(
            run_at=datetime.now(timezone.utc).isoformat(),
            service_url=self.config.service_url,
            endpoint=self.config.endpoint.value,
            total_tests=total,
            passed=passed,
            failed=failed,
            pass_rate=(passed / total) if total else 0.0,
            avg_latency_ms=(sum(latencies) / total) if total else 0.0,
            p95_latency_ms=_percentile(latencies, 0.95),
            cache_hit_rate=(cache_hits / total) if total else 0.0,
            retrieval_failure_rate=retrieval_failure_rate,
            sql_failure_rate=sql_failure_rate,
            provider_failure_rate=provider_failure_rate,
            failure_breakdown=failure_breakdown,
            difficulty_breakdown=by_level,
            suite_breakdown=by_suite,
            output_files={},
        )
