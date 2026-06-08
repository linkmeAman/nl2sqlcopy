from __future__ import annotations

import re
from typing import Any

from nl2sql_service.evaluation.models import (
    BenchmarkCase,
    BenchmarkSqlCharacteristics,
    FailureHints,
    FailureType,
    RunAssessment,
    RunResult,
)

_RETRIEVAL_STAGES = {
    "query_rewrite",
    "schema_retrieval",
    "column_retrieval",
    "vector_search",
    "sql_context_focus",
}
_SQL_STAGES = {"sql_generation", "review_gate"}
_EXECUTION_STAGES = {"execution"}
_ANSWER_STAGES = {"answer_generation"}
_CLARIFICATION_ACTIONS = {"ASK_CLARIFICATION", "REQUEST_CLARIFICATION", "GIVE_UP"}
_PROVIDER_WARNING_CODES = {
    "REQUEST_TIMEOUT",
    "OLLAMA_TIMEOUT",
    "OLLAMA_UPSTREAM",
    "OLLAMA_MALFORMED",
    "ANSWER_TIMEOUT",
    "ANSWER_UPSTREAM",
    "ANSWER_MALFORMED",
}
_VALIDATION_WARNING_CODES = {
    "SQL_EMPTY",
    "SQL_MULTI_STATEMENT",
    "SQL_DESTRUCTIVE",
    "SQL_NOT_SELECT",
    "TABLE_OUT_OF_SCOPE",
    "COLUMN_OUT_OF_SCOPE",
    "MYSQL_EXPLAIN_ERROR",
    "MYSQL_EXPLAIN_UNAVAILABLE",
    "REVIEW_FAILED",
}
_EXECUTION_WARNING_CODES = {"MYSQL_QUERY_ERROR"}
_ANSWER_WARNING_ONLY_CODES = {"ANSWER_HALLUCINATION"}
_RETRIEVAL_FAMILY = {
    FailureType.RETRIEVAL_FAILURE,
    FailureType.CHUNKING_FAILURE,
    FailureType.RERANKING_FAILURE,
    FailureType.SCHEMA_RETRIEVAL_FAILURE,
}


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _event_details_as_text(event: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("message", "stage", "status", "error_source", "provider", "model"):
        value = event.get(key)
        if value:
            parts.append(str(value))
    for key in ("details", "metadata", "input_summary", "output_summary"):
        value = event.get(key)
        if value is not None:
            parts.append(str(value))
    warning_codes = event.get("warning_codes") or []
    if warning_codes:
        parts.append("warning_codes=" + ",".join(str(code) for code in warning_codes))
    errors = event.get("errors") or []
    if errors:
        parts.append("errors=" + ",".join(str(error) for error in errors))
    return "\n".join(parts)


def _step_to_action(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "iteration": step.get("iteration"),
        "action": step.get("action"),
        "action_input": step.get("action_input"),
        "observation": step.get("observation"),
        "duration_ms": step.get("duration_ms"),
    }


def _extract_react_actions(response_body: dict[str, Any]) -> list[dict[str, Any]]:
    react_trace = response_body.get("react_trace")
    if not isinstance(react_trace, dict):
        return []
    steps = react_trace.get("steps") or []
    if not isinstance(steps, list):
        return []
    actions: list[dict[str, Any]] = []
    for step in steps:
        if isinstance(step, dict):
            actions.append(_step_to_action(step))
    return actions


def _extract_response_warnings(response_body: dict[str, Any]) -> list[str]:
    warnings = response_body.get("warnings") or []
    warning_codes: list[str] = []
    for warning in warnings:
        if isinstance(warning, dict):
            code = warning.get("code")
            if code:
                warning_codes.append(str(code))
    return warning_codes


def _extract_trace_summary(run: RunResult) -> dict[str, Any]:
    trace_payload = run.trace_events or []
    response_body = run.response_body or {}
    provider_usage: list[dict[str, Any]] = []
    retrieval_events: list[dict[str, Any]] = []
    sql_events: list[dict[str, Any]] = []
    validation_events: list[dict[str, Any]] = []
    execution_events: list[dict[str, Any]] = []
    answer_events: list[dict[str, Any]] = []
    retrieval_context: list[Any] = []
    matched_groups: list[str] = []
    tables_in_scope: list[str] = []
    selected_tables: list[str] = []
    retrieved_tables: list[str] = []
    context_confidence_score: float | None = None
    context_confidence_details: dict[str, Any] = {}
    cache_hit = bool(run.cache_hit)
    cache_source = run.cache_source
    trace_id = run.trace_id
    last_provider: str | None = None
    last_model: str | None = None
    last_event_text = ""

    def add_unique(target: list[str], values: Any) -> None:
        if isinstance(values, str):
            values = [values]
        for value in values or []:
            normalized = str(value).strip()
            if normalized and normalized not in target:
                target.append(normalized)

    for event in trace_payload:
        if not isinstance(event, dict):
            continue
        stage = str(event.get("stage") or "")
        status = str(event.get("status") or "")
        message = str(event.get("message") or "")
        event_text = _event_details_as_text(event)
        last_event_text = event_text or last_event_text
        if trace_id in {None, "", "-"}:
            candidate_trace_id = event.get("trace_id")
            if candidate_trace_id:
                trace_id = str(candidate_trace_id)
        provider = event.get("provider")
        model = event.get("model")
        if provider:
            last_provider = str(provider)
        if model:
            last_model = str(model)
        if provider or model:
            provider_usage.append(
                {
                    "stage": stage,
                    "status": status,
                    "provider": provider,
                    "model": model,
                }
            )
        if stage in _RETRIEVAL_STAGES:
            retrieval_events.append(
                {
                    "stage": stage,
                    "status": status,
                    "message": message,
                    "warning_codes": list(event.get("warning_codes") or []),
                    "details": event.get("details") or {},
                    "output_summary": event.get("output_summary") or {},
                }
            )
            details = event.get("details") or {}
            output_summary = event.get("output_summary") or {}
            add_unique(retrieval_context, [message, event_text])
            add_unique(matched_groups, output_summary.get("matched_groups"))
            add_unique(tables_in_scope, output_summary.get("selected_tables"))
            add_unique(selected_tables, output_summary.get("selected_tables"))
            add_unique(retrieved_tables, output_summary.get("selected_tables"))
            add_unique(retrieved_tables, details.get("tables_in_scope"))
            add_unique(retrieved_tables, details.get("tables_after_focus"))
            if stage == "sql_context_focus":
                add_unique(selected_tables, details.get("tables_after_focus"))
        elif stage in _SQL_STAGES:
            sql_events.append(
                {
                    "stage": stage,
                    "status": status,
                    "message": message,
                    "warning_codes": list(event.get("warning_codes") or []),
                    "error_source": event.get("error_source"),
                    "details": event.get("details") or {},
                    "output_summary": event.get("output_summary") or {},
                }
            )
            details = event.get("details") or {}
            output_summary = event.get("output_summary") or {}
            add_unique(selected_tables, details.get("tables_used"))
            add_unique(matched_groups, details.get("matched_groups"))
            if stage == "review_gate":
                validation_events.append(sql_events[-1])
            add_unique(retrieved_tables, output_summary.get("tables_used"))
        elif stage in _EXECUTION_STAGES:
            execution_events.append(
                {
                    "stage": stage,
                    "status": status,
                    "message": message,
                    "warning_codes": list(event.get("warning_codes") or []),
                    "error_source": event.get("error_source"),
                    "details": event.get("details") or {},
                }
            )
        elif stage in _ANSWER_STAGES:
            answer_events.append(
                {
                    "stage": stage,
                    "status": status,
                    "message": message,
                    "warning_codes": list(event.get("warning_codes") or []),
                    "error_source": event.get("error_source"),
                    "details": event.get("details") or {},
                }
            )
        elif stage == "react_iteration":
            retrieval_context.append(event.get("details") or {})
            details = event.get("details") or {}
            if isinstance(details, dict):
                action = details.get("action")
                if action:
                    add_unique(selected_tables, details.get("tables_used"))
                    add_unique(matched_groups, details.get("matched_groups"))
                    if details.get("context_confidence_score") is not None:
                        try:
                            context_confidence_score = float(details.get("context_confidence_score"))
                        except Exception:  # noqa: BLE001
                            pass
                    context_confidence_details = details.get("context_confidence_details") or context_confidence_details
        elif stage == "cache_lookup":
            if status == "completed":
                cache_hit = True
                details = event.get("details") or {}
                cache_source = str(details.get("cache_source") or cache_source or "")
        if stage == "complete" and event.get("details"):
            details = event.get("details") or {}
            add_unique(selected_tables, details.get("tables_used"))
            add_unique(matched_groups, details.get("matched_groups"))
        if event.get("warning_codes"):
            pass

    react_actions = _extract_react_actions(response_body)
    if not selected_tables:
        add_unique(selected_tables, response_body.get("tables_used"))
    if not matched_groups:
        add_unique(matched_groups, response_body.get("matched_groups"))
    if not retrieved_tables:
        add_unique(retrieved_tables, response_body.get("tables_used"))
    if context_confidence_score is None:
        react_trace = response_body.get("react_trace")
        if isinstance(react_trace, dict):
            for step in _as_list(react_trace.get("steps")):
                if isinstance(step, dict):
                    details = step.get("details") or {}
                    if isinstance(details, dict) and details.get("context_confidence_score") is not None:
                        try:
                            context_confidence_score = float(details.get("context_confidence_score"))
                        except Exception:  # noqa: BLE001
                            pass
                        context_confidence_details = details.get("context_confidence_details") or context_confidence_details
                        break

    evidence_parts: list[str] = []
    for snippet in (
        run.actual_answer,
        run.generated_sql,
        response_body.get("answer"),
        response_body.get("sql"),
        last_event_text,
    ):
        if snippet:
            evidence_parts.append(str(snippet))
    for action in react_actions:
        for key in ("action", "action_input", "observation"):
            value = action.get(key)
            if value:
                evidence_parts.append(str(value))
    for event in trace_payload:
        if isinstance(event, dict):
            evidence_parts.append(_event_details_as_text(event))

    warning_codes = sorted(
        {
            *map(str, _extract_response_warnings(response_body)),
            *{
                str(code)
                for event in trace_payload
                if isinstance(event, dict)
                for code in (event.get("warning_codes") or [])
                if code
            },
            *{
                str(code)
                for code in (run.warnings or [])
                if isinstance(code, dict) and code.get("code")
            },
        }
    )
    if response_body.get("warnings"):
        warning_codes = sorted(set(warning_codes) | set(_extract_response_warnings(response_body)))

    summary = {
        "trace_id": trace_id,
        "request_id": run.spec.request_id,
        "cache_hit": cache_hit,
        "cache_source": cache_source,
        "provider": last_provider or run.provider,
        "model": last_model or run.model,
        "matched_groups": matched_groups,
        "tables_in_scope": tables_in_scope,
        "selected_tables": selected_tables,
        "retrieved_tables": retrieved_tables,
        "retrieved_context": retrieval_context,
        "context_confidence_score": context_confidence_score,
        "context_confidence_details": context_confidence_details,
        "provider_usage": provider_usage,
        "retrieval_events": retrieval_events,
        "sql_events": sql_events,
        "validation_events": validation_events,
        "execution_events": execution_events,
        "answer_events": answer_events,
        "react_actions": react_actions,
        "react_final_action": (
            response_body.get("react_trace", {}).get("final_action")
            if isinstance(response_body.get("react_trace"), dict)
            else None
        ),
        "react_action_count": len(react_actions),
        "warning_codes": warning_codes,
        "evidence_text": "\n".join(part for part in evidence_parts if part),
    }
    return summary


def _text_contains_all(haystack: str, needles: list[str]) -> bool:
    normalized = _normalize_text(haystack)
    return all(_normalize_text(needle) in normalized for needle in needles)


def _text_missing_any(haystack: str, needles: list[str]) -> list[str]:
    normalized = _normalize_text(haystack)
    missing: list[str] = []
    for needle in needles:
        if _normalize_text(needle) not in normalized:
            missing.append(needle)
    return missing


def _sql_stats(sql: str) -> dict[str, Any]:
    normalized = _normalize_text(sql)
    join_count = len(re.findall(r"\bjoin\b", normalized, flags=re.IGNORECASE))
    limit_match = re.search(r"\blimit\s+(\d+)", normalized, flags=re.IGNORECASE)
    return {
        "has_join": join_count > 0,
        "join_count": join_count,
        "has_group_by": bool(re.search(r"\bgroup\s+by\b", normalized, flags=re.IGNORECASE)),
        "has_order_by": bool(re.search(r"\border\s+by\b", normalized, flags=re.IGNORECASE)),
        "has_limit": bool(limit_match),
        "limit_value": int(limit_match.group(1)) if limit_match else None,
        "has_select_star": bool(re.search(r"\bselect\s+\*", normalized, flags=re.IGNORECASE)),
    }


def _sql_violations(sql: str, expectations: BenchmarkSqlCharacteristics) -> list[str]:
    violations: list[str] = []
    normalized_sql = _normalize_text(sql)
    stats = _sql_stats(sql)
    for needle in expectations.must_include:
        if _normalize_text(needle) not in normalized_sql:
            violations.append(f"missing SQL fragment: {needle}")
    for needle in expectations.must_exclude:
        if _normalize_text(needle) in normalized_sql:
            violations.append(f"forbidden SQL fragment present: {needle}")
    for table in expectations.must_use_tables:
        if _normalize_text(table) not in normalized_sql:
            violations.append(f"expected table not present in SQL: {table}")
    if expectations.forbid_select_star and stats["has_select_star"]:
        violations.append("SELECT * should not be used")
    if expectations.requires_join is True and not stats["has_join"]:
        violations.append("JOIN expected but not present")
    if expectations.requires_join is False and stats["has_join"]:
        violations.append("JOIN present but not expected")
    if expectations.min_join_count is not None and stats["join_count"] < expectations.min_join_count:
        violations.append(
            f"JOIN count below minimum: expected >= {expectations.min_join_count}, got {stats['join_count']}"
        )
    if expectations.max_join_count is not None and stats["join_count"] > expectations.max_join_count:
        violations.append(
            f"JOIN count above maximum: expected <= {expectations.max_join_count}, got {stats['join_count']}"
        )
    if expectations.requires_group_by is True and not stats["has_group_by"]:
        violations.append("GROUP BY expected but not present")
    if expectations.requires_group_by is False and stats["has_group_by"]:
        violations.append("GROUP BY present but not expected")
    if expectations.requires_order_by is True and not stats["has_order_by"]:
        violations.append("ORDER BY expected but not present")
    if expectations.requires_order_by is False and stats["has_order_by"]:
        violations.append("ORDER BY present but not expected")
    if expectations.requires_limit is True and not stats["has_limit"]:
        violations.append("LIMIT expected but not present")
    if expectations.requires_limit is False and stats["has_limit"]:
        violations.append("LIMIT present but not expected")
    if expectations.limit_value is not None and stats["limit_value"] != expectations.limit_value:
        violations.append(
            f"LIMIT value mismatch: expected {expectations.limit_value}, got {stats['limit_value']}"
        )
    for pattern in expectations.regex:
        if not re.search(pattern, sql, flags=re.IGNORECASE | re.DOTALL):
            violations.append(f"SQL regex did not match: {pattern}")
    return violations


def _status_reasons(case: BenchmarkCase, run: RunResult) -> list[str]:
    response = run.response_body or {}
    expected_status = case.expected_criteria.status
    actual_status = str(response.get("status") or run.response_status or "").strip() or None
    reasons: list[str] = []
    if actual_status != expected_status:
        reasons.append(f"status mismatch expected={expected_status} actual={actual_status or 'missing'}")
    return reasons


def _warning_codes(run: RunResult) -> list[str]:
    response = run.response_body or {}
    codes = set(_extract_response_warnings(response))
    for warning in run.warnings:
        if isinstance(warning, dict) and warning.get("code"):
            codes.add(str(warning["code"]))
    for event in run.trace_events:
        if isinstance(event, dict):
            for code in event.get("warning_codes") or []:
                if code:
                    codes.add(str(code))
    return sorted(codes)


def _provider_failure_detected(run: RunResult, warning_codes: list[str]) -> bool:
    if any(code in _PROVIDER_WARNING_CODES for code in warning_codes):
        return True
    for event in run.trace_events:
        if not isinstance(event, dict):
            continue
        if event.get("provider") or event.get("model"):
            continue
        error_source = str(event.get("error_source") or "").lower()
        if error_source in {"provider", "generation_transport", "embedding", "service_timeout"}:
            if str(event.get("status") or "").lower() in {"failed", "warning"}:
                return True
    return False


def _stage_failure_detected(run: RunResult, stage_names: set[str]) -> bool:
    for event in run.trace_events:
        if not isinstance(event, dict):
            continue
        if str(event.get("stage") or "") not in stage_names:
            continue
        if str(event.get("status") or "").lower() in {"failed", "warning", "needs_context"}:
            return True
    return False


def _select_failure_type(
    case: BenchmarkCase,
    run: RunResult,
    reasons: list[str],
    trace_summary: dict[str, Any],
    sql_violations: list[str],
    keyword_missing: list[str],
    answer_missing: list[str],
) -> FailureType:
    warning_codes = _warning_codes(run)
    response = run.response_body or {}
    actual_status = str(response.get("status") or run.response_status or "").strip() or None
    expected_status = case.expected_criteria.status

    if run.request_error:
        return FailureType.PROVIDER_FAILURE

    if run.http_status_code is not None and run.http_status_code >= 400:
        if _stage_failure_detected(run, _EXECUTION_STAGES):
            return FailureType.EXECUTION_FAILURE
        if _stage_failure_detected(run, _ANSWER_STAGES):
            return FailureType.ANSWER_GENERATION_FAILURE
        if _stage_failure_detected(run, _SQL_STAGES):
            return FailureType.SQL_GENERATION_FAILURE
        return FailureType.PROVIDER_FAILURE

    if _provider_failure_detected(run, warning_codes):
        return FailureType.PROVIDER_FAILURE

    if trace_summary.get("cache_hit") and reasons:
        return FailureType.CACHE_FAILURE

    if any(code in _EXECUTION_WARNING_CODES for code in warning_codes):
        return FailureType.EXECUTION_FAILURE

    if any(code in _VALIDATION_WARNING_CODES for code in warning_codes):
        return FailureType.SQL_VALIDATION_FAILURE

    if any(code in _ANSWER_WARNING_ONLY_CODES for code in warning_codes):
        return FailureType.ANSWER_GENERATION_FAILURE

    if actual_status == "clarification_needed" and expected_status == "ok":
        react_final = str(trace_summary.get("react_final_action") or "")
        if react_final in _CLARIFICATION_ACTIONS:
            return FailureType.PLANNING_FAILURE
        return FailureType.SQL_GENERATION_FAILURE

    if actual_status == "rejected" and expected_status == "ok":
        if _stage_failure_detected(run, _SQL_STAGES):
            return FailureType.SQL_GENERATION_FAILURE
        if _stage_failure_detected(run, _EXECUTION_STAGES):
            return FailureType.EXECUTION_FAILURE
        if _stage_failure_detected(run, _ANSWER_STAGES):
            return FailureType.ANSWER_GENERATION_FAILURE

    if keyword_missing or sql_violations:
        retrieved_tables = set(str(table).lower() for table in trace_summary.get("retrieved_tables", []))
        selected_tables = set(str(table).lower() for table in trace_summary.get("selected_tables", []))
        expected_tables = {table.lower() for table in case.expected_tables}
        if expected_tables and expected_tables.isdisjoint(retrieved_tables):
            hinted = _hint_failure_type(case.failure_classification_hints, default=FailureType.SCHEMA_RETRIEVAL_FAILURE)
            if hinted in _RETRIEVAL_FAMILY:
                if hinted == FailureType.CHUNKING_FAILURE:
                    return hinted
                if hinted == FailureType.RERANKING_FAILURE and selected_tables & expected_tables:
                    return hinted
            if not trace_summary.get("retrieval_events"):
                return FailureType.RETRIEVAL_FAILURE
            return FailureType.SCHEMA_RETRIEVAL_FAILURE
        if expected_tables and expected_tables & retrieved_tables and expected_tables.isdisjoint(selected_tables):
            hinted = _hint_failure_type(case.failure_classification_hints, default=FailureType.RERANKING_FAILURE)
            if hinted in _RETRIEVAL_FAMILY:
                return hinted
            return FailureType.RERANKING_FAILURE
        if case.failure_classification_hints and FailureType.CHUNKING_FAILURE in case.failure_classification_hints.likely_failure_types:
            return FailureType.CHUNKING_FAILURE
        if sql_violations:
            return FailureType.SQL_GENERATION_FAILURE
        if keyword_missing:
            return FailureType.RETRIEVAL_FAILURE

    if answer_missing:
        return FailureType.ANSWER_GENERATION_FAILURE

    if actual_status != expected_status:
        if expected_status == "ok":
            return FailureType.PLANNING_FAILURE
        return FailureType.SQL_VALIDATION_FAILURE

    if _stage_failure_detected(run, _ANSWER_STAGES):
        return FailureType.ANSWER_GENERATION_FAILURE

    return FailureType.SQL_GENERATION_FAILURE


def _hint_failure_type(hints: FailureHints, *, default: FailureType) -> FailureType:
    if hints.likely_failure_types:
        return hints.likely_failure_types[0]
    return default


def _recommended_investigation(case: BenchmarkCase, failure_type: FailureType) -> str:
    if case.failure_classification_hints.investigation:
        return case.failure_classification_hints.investigation
    mapping = {
        FailureType.RETRIEVAL_FAILURE: "Inspect embedding quality, chunking, and top-k recall for the missing schema or fact.",
        FailureType.CHUNKING_FAILURE: "Inspect chunk boundaries and whether the target fact is split across adjacent chunks.",
        FailureType.RERANKING_FAILURE: "Inspect similarity ranking and reranker ordering for the retrieved chunks.",
        FailureType.SCHEMA_RETRIEVAL_FAILURE: "Inspect schema-group retrieval, table selection, and column refresh signals.",
        FailureType.PLANNING_FAILURE: "Inspect the ReAct action sequence, available-actions gating, and context-confidence threshold.",
        FailureType.SQL_GENERATION_FAILURE: "Inspect the generated SQL against the prompt context, joins, filters, and table scope.",
        FailureType.SQL_VALIDATION_FAILURE: "Inspect governance review, syntax/safety validation, and EXPLAIN/review failures.",
        FailureType.EXECUTION_FAILURE: "Inspect MySQL execution errors, row-cap behavior, and query runtime assumptions.",
        FailureType.ANSWER_GENERATION_FAILURE: "Inspect answer prompting, row/column context, and hallucination guards.",
        FailureType.CACHE_FAILURE: "Inspect cache key normalization, cache epoch, and whether stale results were reused.",
        FailureType.PROVIDER_FAILURE: "Inspect provider timeout, malformed output, and fallback exhaustion for the active model role.",
    }
    return mapping[failure_type]


def assess_run(case: BenchmarkCase, run: RunResult) -> RunAssessment:
    response = run.response_body or {}
    trace_summary = _extract_trace_summary(run)
    run.trace_summary = trace_summary
    run.retrieved_context = list(trace_summary.get("retrieved_context") or trace_summary.get("retrieval_events") or [])
    run.retrieved_tables = list(trace_summary.get("retrieved_tables") or [])
    run.react_actions = list(trace_summary.get("react_actions") or _extract_react_actions(response))
    run.provider = str(trace_summary.get("provider") or run.provider or "") or None
    run.model = str(trace_summary.get("model") or run.model or "") or None
    run.cache_hit = bool(trace_summary.get("cache_hit") or run.cache_hit)
    run.cache_source = str(trace_summary.get("cache_source") or run.cache_source or "") or None
    run.trace_id = str(trace_summary.get("trace_id") or run.trace_id or "") or None

    actual_status = str(response.get("status") or run.response_status or "").strip() or None
    expected_status = case.expected_criteria.status
    sql = str(response.get("sql") or run.generated_sql or "")
    answer = str(response.get("answer") or run.actual_answer or "")
    evidence_text = str(trace_summary.get("evidence_text") or "")
    warnings = _warning_codes(run)
    if response.get("warnings"):
        for warning in response.get("warnings") or []:
            if isinstance(warning, dict):
                code = warning.get("code")
                if code:
                    warnings.append(str(code))
    warnings = sorted(set(warnings))

    status_reasons = _status_reasons(case, run)
    keyword_missing = [
        keyword
        for keyword in case.expected_keywords
        if _normalize_text(keyword) not in _normalize_text("\n".join([answer, sql, evidence_text]))
    ]
    answer_missing = _text_missing_any(answer, case.expected_criteria.answer_contains)
    answer_forbidden = [
        keyword
        for keyword in case.expected_criteria.answer_not_contains
        if _normalize_text(keyword) in _normalize_text(answer)
    ]
    react_actions = run.react_actions or _extract_react_actions(response)
    react_action_names = [
        str(action.get("action") or "")
        for action in react_actions
        if isinstance(action, dict) and action.get("action")
    ]
    react_missing = [
        action
        for action in case.expected_criteria.react_actions_contains
        if action not in react_action_names
    ]
    react_forbidden = [
        action
        for action in case.expected_criteria.react_actions_not_contains
        if action in react_action_names
    ]
    warning_missing = [
        code
        for code in case.expected_criteria.warning_codes_contains
        if code not in warnings
    ]
    warning_forbidden = [
        code
        for code in case.expected_criteria.warning_codes_not_contains
        if code in warnings
    ]
    sql_violations = _sql_violations(sql, case.expected_sql_characteristics) if sql else []
    tables_used = {str(table).lower() for table in (response.get("tables_used") or run.tables_used or [])}
    matched_groups = {str(group).lower() for group in (response.get("matched_groups") or run.matched_groups or [])}
    selected_tables = {str(table).lower() for table in trace_summary.get("selected_tables", [])}
    retrieved_tables = {str(table).lower() for table in trace_summary.get("retrieved_tables", [])}
    expected_tables = {table.lower() for table in case.expected_tables}
    missing_tables = sorted(expected_tables - (tables_used | retrieved_tables | selected_tables))
    tables_shortfall = []
    if expected_tables and missing_tables:
        tables_shortfall.append(f"missing expected tables: {', '.join(missing_tables)}")
    if case.expected_sql_characteristics.must_use_tables:
        required_tables_missing = [
            table
            for table in case.expected_sql_characteristics.must_use_tables
            if _normalize_text(table) not in _normalize_text(sql)
        ]
        if required_tables_missing:
            tables_shortfall.append(
                f"SQL missing expected table references: {', '.join(required_tables_missing)}"
            )

    reasons: list[str] = []
    reasons.extend(status_reasons)
    if keyword_missing:
        reasons.append(f"missing expected keyword(s): {', '.join(keyword_missing)}")
    if answer_missing:
        reasons.append(f"answer missing expected text: {', '.join(answer_missing)}")
    if answer_forbidden:
        reasons.append(f"answer contained forbidden text: {', '.join(answer_forbidden)}")
    if react_missing:
        reasons.append(f"missing expected ReAct action(s): {', '.join(react_missing)}")
    if react_forbidden:
        reasons.append(f"unexpected ReAct action(s) present: {', '.join(react_forbidden)}")
    if warning_missing:
        reasons.append(f"missing expected warning code(s): {', '.join(warning_missing)}")
    if warning_forbidden:
        reasons.append(f"unexpected warning code(s) present: {', '.join(warning_forbidden)}")
    reasons.extend(sql_violations)
    reasons.extend(tables_shortfall)

    passed = not reasons
    failure_type = None
    root_cause = None
    recommended_investigation = None
    failure_signals: list[str] = []

    if not passed:
        failure_type = _select_failure_type(
            case,
            run,
            reasons,
            trace_summary,
            sql_violations,
            keyword_missing,
            answer_missing,
        )
        root_cause = "; ".join(reasons[:3])
        if not root_cause:
            root_cause = failure_type.value if failure_type else "unknown"
        recommended_investigation = _recommended_investigation(case, failure_type)
        if trace_summary.get("cache_hit"):
            failure_signals.append("cache_hit=true")
        if run.http_status_code is not None:
            failure_signals.append(f"http_status_code={run.http_status_code}")
        if run.request_error:
            failure_signals.append(f"request_error={run.request_error}")
        if trace_summary.get("react_final_action"):
            failure_signals.append(f"react_final_action={trace_summary['react_final_action']}")
        if trace_summary.get("context_confidence_score") is not None:
            failure_signals.append(
                f"context_confidence_score={trace_summary['context_confidence_score']}"
            )
        if warnings:
            failure_signals.append("warning_codes=" + ",".join(warnings))
        if trace_summary.get("retrieved_tables"):
            failure_signals.append(
                "retrieved_tables=" + ",".join(str(table) for table in trace_summary["retrieved_tables"])
            )
        if trace_summary.get("selected_tables"):
            failure_signals.append(
                "selected_tables=" + ",".join(str(table) for table in trace_summary["selected_tables"])
            )
        if matched_groups:
            failure_signals.append("matched_groups=" + ",".join(sorted(matched_groups)))
        if case.failure_classification_hints.signals:
            failure_signals.extend(case.failure_classification_hints.signals)

    assessment = RunAssessment(
        passed=passed,
        reasons=reasons,
        failure_type=failure_type,
        root_cause=root_cause,
        recommended_investigation=recommended_investigation,
        trace_summary=trace_summary,
        retrieved_context=list(trace_summary.get("retrieved_context") or []),
        retrieved_tables=sorted(set(run.retrieved_tables)),
        react_actions=react_actions,
        provider=run.provider,
        model=run.model,
        cache_hit=run.cache_hit,
        cache_source=run.cache_source,
        failure_signals=failure_signals,
    )
    run.assessment = assessment
    return assessment


def build_failure_record(case: BenchmarkCase, run: RunResult) -> dict[str, Any]:
    assessment = run.assessment or assess_run(case, run)
    expected = {
        "status": case.expected_criteria.status,
        "tables": case.expected_tables,
        "keywords": case.expected_keywords,
        "sql_characteristics": case.expected_sql_characteristics.model_dump(mode="json"),
        "criteria": case.expected_criteria.model_dump(mode="json"),
        "failure_hints": case.failure_classification_hints.model_dump(mode="json"),
    }
    record = {
        "timestamp": run.timestamp,
        "test_id": case.id,
        "difficulty": f"level-{run.spec.suite_level}",
        "suite_id": run.spec.suite_id,
        "request_id": run.spec.request_id,
        "trace_id": assessment.trace_summary.get("trace_id") or run.trace_id,
        "query": case.query,
        "expected": expected,
        "actual_answer": run.actual_answer,
        "generated_sql": run.generated_sql,
        "retrieved_context": assessment.retrieved_context,
        "retrieved_tables": assessment.retrieved_tables,
        "react_actions": assessment.react_actions,
        "provider": assessment.provider,
        "model": assessment.model,
        "cache_hit": assessment.cache_hit,
        "cache_source": assessment.cache_source,
        "latency_ms": run.latency_ms,
        "http_status_code": run.http_status_code,
        "failure_type": assessment.failure_type.value if assessment.failure_type else None,
        "root_cause": assessment.root_cause,
        "trace_summary": assessment.trace_summary,
        "recommended_investigation": assessment.recommended_investigation,
        "warnings": run.warnings,
        "failure_signals": assessment.failure_signals,
        "response_status": run.response_status,
        "row_count": run.row_count,
        "columns": run.columns,
        "matched_groups": run.matched_groups,
        "attempt_count": run.attempt_count,
        "stream_events": run.stream_events,
        "trace_events": run.trace_events,
        "request_error": run.request_error,
        "failure_log_entry": run.failure_log_entry,
        "pass_reasons": assessment.reasons,
    }
    return record
