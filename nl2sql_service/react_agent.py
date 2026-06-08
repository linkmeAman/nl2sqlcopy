from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable

import asyncpg

from nl2sql_service.column_loader import load_columns_for_tables
from nl2sql_service import pattern_store
from nl2sql_service import query_rewriter
from nl2sql_service.config import Settings, settings as default_settings
from nl2sql_service.instruction_store import (
    record_instruction_outcome,
    retrieve_similar_corrections,
)
from nl2sql_service.llm import get_model_client
from nl2sql_service.models import (
    GenerateSqlClarification,
    GenerateSqlRejected,
    GenerateSqlResponse,
    GenerateSqlSuccess,
    ReActAction,
    ReActStep,
    ReactTrace,
    SqlWarning,
    WarningCode,
)
from nl2sql_service.observability.sanitization import summarize_text
from nl2sql_service.roles import LLMRole
from nl2sql_service.rulebook import build_governance_block, get_config
from nl2sql_service.retrieve import retrieve, retrieve_column_catalog, retrieve_groups
from nl2sql_service.sql_generator import (
    PgVectorStore,
    VectorStore,
    _build_schema_derived_suggestions,
    _load_query_embedding,
    build_refinement_prompt,
    build_sql_prompt,
    call_ollama,
    extract_sql,
    narrow_select_star,
    run_explain,
    validate_columns_used,
    validate_sql_safety,
    validate_tables_used,
)

TraceCallback = Callable[..., Awaitable[None]]


async def _emit_trace(
    trace: TraceCallback | None,
    *,
    stage: str,
    status: str,
    message: str,
    duration_ms: int | None = None,
    warning_codes: list[str] | None = None,
    error_source: str | None = None,
    details: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    if trace is None:
        return
    await trace(
        stage=stage,
        status=status,
        message=message,
        duration_ms=duration_ms,
        warning_codes=warning_codes,
        error_source=error_source,
        details=details or {},
        **extra,
    )


def _normalize_table_name(value: str) -> str:
    return value.strip().strip("`\"[]").lower()


def _parse_csv_items(value: str) -> list[str]:
    items: list[str] = []
    for raw_item in value.split(","):
        item = _normalize_table_name(raw_item)
        if item and item not in items:
            items.append(item)
    return items


def _parse_csv_terms(value: str) -> set[str]:
    return {
        item.strip().lower()
        for item in value.split(",")
        if item.strip()
    }


def _ensure_state_defaults(state: dict[str, Any]) -> None:
    state.setdefault("context", "")
    state.setdefault("tables_in_scope", [])
    state.setdefault("matched_groups", [])
    state.setdefault("allowed_columns", {})
    state.setdefault("current_sql", None)
    state.setdefault("last_validation_errors", [])
    state.setdefault("sql_generation_count", 0)
    state.setdefault("tables_used", [])
    state.setdefault("top_k", default_settings.top_k)
    state.setdefault("search_query", "")
    state.setdefault("actions_taken", [])
    state.setdefault("retrieved_tables", set())
    state.setdefault("retrieved_schema", {})
    state.setdefault("past_corrections", [])
    state.setdefault("past_corrections_checked", False)
    state.setdefault("learned_instructions", [])
    state.setdefault("sample_queries", [])
    state.setdefault("join_paths", [])
    state.setdefault("context_confidence_score", 0.0)
    state.setdefault("context_confidence_details", {})
    state.setdefault("columns_refreshed_tables", set())
    state.setdefault("generation_tables_in_scope", [])
    state.setdefault("query_embedding", None)
    state.setdefault("focus_tables", [])
    state.setdefault("deadline_monotonic", None)


def _merge_ordered_strings(existing: list[str], incoming: list[str]) -> list[str]:
    merged: list[str] = []
    for item in [*existing, *incoming]:
        normalized = str(item).strip()
        if normalized and normalized not in merged:
            merged.append(normalized)
    return merged


def _merge_allowed_columns(
    existing: dict[str, list[str]],
    incoming: dict[str, list[str]],
) -> dict[str, list[str]]:
    merged = {str(table): list(columns) for table, columns in existing.items()}
    for table, columns in incoming.items():
        table_key = _normalize_table_name(str(table))
        if not table_key:
            continue
        merged[table_key] = _merge_ordered_strings(
            merged.get(table_key, []),
            [str(column).strip() for column in columns if str(column).strip()],
        )
    return merged


def _merge_context_block(current: str, new_block: str) -> str:
    stripped_new = new_block.strip()
    if not stripped_new:
        return current
    stripped_current = current.strip()
    if not stripped_current:
        return stripped_new
    if stripped_new in stripped_current:
        return stripped_current
    return f"{stripped_current}\n\n{stripped_new}"


def _generation_tables_in_scope(state: dict[str, Any]) -> list[str]:
    focused = [
        _normalize_table_name(table)
        for table in state.get("generation_tables_in_scope", [])
        if str(table).strip()
    ]
    if focused:
        return focused
    return [
        _normalize_table_name(table)
        for table in state.get("tables_in_scope", [])
        if str(table).strip()
    ]


def _generation_allowed_columns(state: dict[str, Any]) -> dict[str, list[str]]:
    generation_tables = set(_generation_tables_in_scope(state))
    allowed_columns = state.get("allowed_columns") or {}
    if not generation_tables:
        return allowed_columns
    return {
        _normalize_table_name(table): list(columns)
        for table, columns in allowed_columns.items()
        if _normalize_table_name(str(table)) in generation_tables
    }


def _remaining_stage_budget_seconds(state: dict[str, Any]) -> float | None:
    deadline = state.get("deadline_monotonic")
    if deadline is None:
        return None
    return max(0.0, float(deadline) - time.monotonic())


def _table_name_variants(table: str) -> set[str]:
    normalized = _normalize_table_name(table)
    if not normalized:
        return set()
    spaced = normalized.replace("_", " ")
    variants = {normalized, spaced}
    for value in (normalized, spaced):
        if value.endswith("ies"):
            variants.add(f"{value[:-3]}y")
        elif value.endswith("s"):
            variants.add(value[:-1])
        else:
            variants.add(f"{value}s")
    return {variant.strip() for variant in variants if variant.strip()}


def _infer_focus_tables(query: str, candidate_tables: list[str], limit: int = 2) -> list[str]:
    normalized_query = query.lower()
    matched: list[str] = []
    for table in candidate_tables:
        normalized_table = _normalize_table_name(table)
        variants = _table_name_variants(normalized_table)
        if any(
            re.search(rf"\b{re.escape(variant)}\b", normalized_query)
            for variant in variants
        ):
            if normalized_table not in matched:
                matched.append(normalized_table)
    if matched:
        return matched[:limit]
    return []


def _should_check_past_corrections(query: str, settings: Settings) -> bool:
    normalized = " ".join(query.lower().split())
    tokens = [token for token in re.findall(r"[a-z0-9_]+", normalized) if token]
    if len(tokens) >= settings.react_past_corrections_min_tokens:
        return True
    connector_terms = _parse_csv_terms(settings.react_past_corrections_connector_terms)
    if not connector_terms:
        return False
    connector_pattern = "|".join(re.escape(term) for term in sorted(connector_terms))
    return bool(re.search(rf"\b(?:{connector_pattern})\b", normalized))


async def _ensure_query_embedding(
    *,
    query: str,
    state: dict[str, Any],
) -> list[float] | None:
    embedding = state.get("query_embedding")
    if embedding is not None:
        return embedding
    try:
        embedding = await _load_query_embedding(query)
    except Exception:  # noqa: BLE001
        embedding = None
    state["query_embedding"] = embedding
    return embedding


async def _focused_tables_for_generation(
    tables_in_scope: list[str],
    query_embedding: list[float],
    vector_store: VectorStore,
    max_tables: int,
) -> tuple[list[str], int]:
    """
    Focus SQL generation on the most relevant in-scope tables using column-level
    similarity hits from the query embedding.
    """
    normalized_tables = []
    seen_tables: set[str] = set()
    for table in tables_in_scope:
        normalized = _normalize_table_name(table)
        if normalized and normalized not in seen_tables:
            normalized_tables.append(normalized)
            seen_tables.add(normalized)
    if not normalized_tables or len(normalized_tables) <= max_tables:
        return normalized_tables, 0

    search_limit = max(max_tables * 8, len(normalized_tables) * 4)
    hits = await vector_store.search_columns(
        embedding=query_embedding,
        top_k=search_limit,
    )

    table_scores: dict[str, float] = {}
    first_hit_order: dict[str, int] = {}
    column_hits_used = 0
    for index, hit in enumerate(hits):
        table_name = _normalize_table_name(hit.table_name)
        if table_name not in seen_tables:
            continue
        column_hits_used += 1
        first_hit_order.setdefault(table_name, index)
        table_scores[table_name] = max(
            float(hit.similarity),
            table_scores.get(table_name, float("-inf")),
        )

    if not table_scores:
        return normalized_tables[:max_tables], 0

    original_order = {table: index for index, table in enumerate(normalized_tables)}
    focused_tables = [
        table_name
        for table_name, _score in sorted(
            table_scores.items(),
            key=lambda item: (
                -item[1],
                first_hit_order[item[0]],
                original_order[item[0]],
            ),
        )
    ]
    return focused_tables[:max_tables], column_hits_used


def _action_target(action: ReActAction, action_input: str, state: dict[str, Any]) -> str:
    if action in {
        ReActAction.RETRIEVE_MORE_CONTEXT,
        ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
        ReActAction.FETCH_SCHEMA,
    }:
        targets = _parse_csv_items(action_input)
        if not targets:
            targets = [_normalize_table_name(table) for table in state.get("tables_in_scope", [])]
        return ",".join(targets)
    if action == ReActAction.RETRIEVE_JOIN_PATHS:
        targets = sorted(_normalize_table_name(table) for table in state.get("tables_in_scope", []) if table)
        return ",".join(targets)
    if action in {
        ReActAction.RETRIEVE_SAMPLE_QUERIES,
        ReActAction.RETRIEVE_PAST_CORRECTIONS,
    }:
        return " ".join(str(action_input or state.get("search_query") or "").split()).lower()
    return action.value


def apply_iteration_memory_guard(
    action: ReActAction,
    action_input: str,
    state: dict[str, Any],
) -> tuple[ReActAction, str]:
    _ensure_state_defaults(state)
    target = _action_target(action, action_input, state)
    actions_taken = {
        (str(previous_action), str(previous_target))
        for previous_action, previous_target in state.get("actions_taken", [])
    }
    retrieved_tables = {
        _normalize_table_name(table)
        for table in state.get("retrieved_tables", set())
        if str(table).strip()
    }

    if action in {
        ReActAction.RETRIEVE_MORE_CONTEXT,
        ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
        ReActAction.FETCH_SCHEMA,
    }:
        targets = [item for item in _parse_csv_items(target) if item]
        if targets and all(item in retrieved_tables for item in targets):
            fallback = (
                ReActAction.GENERATE_SQL
                if state.get("tables_in_scope")
                else ReActAction.REQUEST_CLARIFICATION
            )
            return fallback, (
                "Duplicate schema retrieval blocked because the requested table set "
                "was already retrieved in this session."
            )

    if (action.value, target) in actions_taken:
        fallback = (
            ReActAction.GENERATE_SQL
            if state.get("tables_in_scope")
            else ReActAction.REQUEST_CLARIFICATION
        )
        return fallback, "Duplicate planner action blocked by the iteration memory guard."

    return action, ""


def compute_context_confidence_score(
    *,
    state: dict[str, Any],
    iteration_count: int,
    settings: Settings,
) -> tuple[float, dict[str, float]]:
    focus_tables = {
        _normalize_table_name(table)
        for table in state.get("focus_tables", [])
        if str(table).strip()
    }
    tables_in_scope = {
        _normalize_table_name(table)
        for table in state.get("tables_in_scope", [])
        if str(table).strip()
    }
    confidence_tables = focus_tables or tables_in_scope
    retrieved_schema = {
        _normalize_table_name(table): columns
        for table, columns in (state.get("retrieved_schema") or {}).items()
        if str(table).strip()
    }
    covered_tables = confidence_tables.intersection(retrieved_schema)
    table_coverage_ratio = (
        len(covered_tables) / len(confidence_tables)
        if confidence_tables
        else 0.0
    )

    join_paths = list(state.get("join_paths") or [])
    expected_join_edges = max(1, len(confidence_tables) - 1) if len(confidence_tables) > 1 else 1
    join_path_score = (
        min(1.0, len(join_paths) / expected_join_edges)
        if len(confidence_tables) > 1
        else 1.0 if confidence_tables else 0.0
    )

    matched_groups = list(state.get("matched_groups") or [])
    target_group_budget = max(1, int(state.get("top_k") or settings.top_k or 1))
    matched_group_score = min(1.0, len(matched_groups) / target_group_budget) if matched_groups else 0.0

    prior_examples = list(state.get("sample_queries") or [])
    past_corrections = list(state.get("past_corrections") or [])
    prior_example_score = 1.0 if prior_examples or past_corrections else 0.0

    raw_score = (
        table_coverage_ratio * settings.react_confidence_tables_weight
        + join_path_score * settings.react_confidence_join_paths_weight
        + matched_group_score * settings.react_confidence_group_weight
        + prior_example_score * settings.react_confidence_example_weight
    )
    iteration_penalty = max(0, iteration_count - 1) * settings.react_confidence_iteration_penalty
    score = max(0.0, min(1.0, raw_score - iteration_penalty))
    return score, {
        "table_coverage_ratio": round(table_coverage_ratio, 4),
        "focus_table_count": float(len(confidence_tables)),
        "join_path_score": round(join_path_score, 4),
        "matched_group_score": round(matched_group_score, 4),
        "prior_example_score": round(prior_example_score, 4),
        "iteration_penalty": round(iteration_penalty, 4),
        "score": round(score, 4),
    }


def _build_available_actions(
    state: dict[str, Any],
    iteration: int,
    settings: Settings,
) -> list[ReActAction]:
    _ensure_state_defaults(state)
    if iteration == 1 and not state.get("past_corrections_checked"):
        return [ReActAction.RETRIEVE_PAST_CORRECTIONS]

    score = float(state.get("context_confidence_score") or 0.0)
    if score >= settings.react_confidence_threshold and state.get("tables_in_scope"):
        return [
            ReActAction.GENERATE_SQL,
            ReActAction.REQUEST_CLARIFICATION,
            ReActAction.GIVE_UP,
        ]

    actions: list[ReActAction] = []
    tables_in_scope = list(state.get("tables_in_scope") or [])
    focus_tables = list(state.get("focus_tables") or [])
    confidence_tables = focus_tables or tables_in_scope
    retrieved_schema = state.get("retrieved_schema") or {}
    retrieved_keys = {_normalize_table_name(name) for name in retrieved_schema.keys()}
    uncovered_tables = [
        table for table in confidence_tables
        if _normalize_table_name(table) not in retrieved_keys
    ]
    if not confidence_tables or uncovered_tables:
        actions.append(ReActAction.RETRIEVE_SCHEMA_FOR_TABLES)

    if len(focus_tables) > 1 and not state.get("join_paths"):
        actions.append(ReActAction.RETRIEVE_JOIN_PATHS)

    ambiguity_high = (
        score < settings.react_confidence_threshold
        and not state.get("sample_queries")
        and (
            len(focus_tables) > 1
            or not state.get("matched_groups")
            or bool(state.get("last_validation_errors"))
        )
    )
    if ambiguity_high:
        actions.append(ReActAction.RETRIEVE_SAMPLE_QUERIES)

    if confidence_tables:
        actions.append(ReActAction.GENERATE_SQL)

    actions.append(ReActAction.REQUEST_CLARIFICATION)
    actions.append(ReActAction.GIVE_UP)

    deduped: list[ReActAction] = []
    for action in actions:
        if action not in deduped:
            deduped.append(action)
    return deduped


def _action_description(action: ReActAction) -> str:
    descriptions = {
        ReActAction.RETRIEVE_PAST_CORRECTIONS: "search verified corrections for similar queries",
        ReActAction.RETRIEVE_SCHEMA_FOR_TABLES: "retrieve targeted table summaries and columns from the vector store",
        ReActAction.RETRIEVE_JOIN_PATHS: "retrieve join paths between in-scope tables from stored relation metadata",
        ReActAction.RETRIEVE_SAMPLE_QUERIES: "retrieve prior successful patterns or sample queries for ambiguous questions",
        ReActAction.REQUEST_CLARIFICATION: "ask the user for more specificity when the context budget is exhausted",
        ReActAction.RETRIEVE_MORE_CONTEXT: "legacy alias for targeted schema retrieval",
        ReActAction.FETCH_SCHEMA: "legacy alias for targeted schema retrieval",
        ReActAction.GENERATE_SQL: "generate a SQL query from the current context",
        ReActAction.VALIDATE_AND_RETURN: "validate the current SQL and return it when safe",
        ReActAction.ASK_CLARIFICATION: "legacy alias for clarification",
        ReActAction.GIVE_UP: "stop when the error is unrecoverable",
    }
    return descriptions.get(action, action.value.replace("_", " ").lower())


def _extract_join_sql(content: str) -> str | None:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("join sql:"):
            return stripped.split(":", 1)[1].strip()
    return None


def _extract_columns_from_results(
    results: list[Any],
    target_tables: list[str],
) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    normalized_targets = {
        _normalize_table_name(table)
        for table in target_tables
        if str(table).strip()
    }
    if not normalized_targets:
        return parsed

    table_line = re.compile(r"^\s*([A-Za-z0-9_]+)\s*:\s*(.+)$")
    for result in results:
        content = str(getattr(result, "content", "") or "")
        metadata = getattr(result, "metadata", {}) or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        metadata = dict(metadata)

        object_name = _normalize_table_name(str(metadata.get("object_name") or metadata.get("table_name") or ""))
        if object_name in normalized_targets and content:
            _, _, columns_text = content.partition(":")
            candidate_columns = []
            for column in columns_text.split(","):
                stripped = re.split(r"\s*[\[(]", column, maxsplit=1)[0].strip().strip("`\"[]")
                if stripped:
                    candidate_columns.append(stripped)
            if candidate_columns:
                parsed[object_name] = candidate_columns

        for line in content.splitlines():
            match = table_line.match(line)
            if not match:
                continue
            table_name = _normalize_table_name(match.group(1))
            if table_name not in normalized_targets:
                continue
            column_block = match.group(2).strip()
            if not column_block or column_block.startswith("("):
                continue
            parsed[table_name] = []
            for column in column_block.split(","):
                stripped = re.split(r"\s*[\[(]", column, maxsplit=1)[0].strip().strip("`\"[]")
                if stripped:
                    parsed[table_name].append(stripped)
    return parsed


def _extract_columns_from_column_results(results: list[Any]) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for result in results:
        metadata = getattr(result, "metadata", {}) or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        metadata = dict(metadata)

        table_name = _normalize_table_name(
            str(metadata.get("table_name") or metadata.get("object_name") or "")
        )
        column_name = str(metadata.get("column_name") or "").strip().strip("`\"[]")
        if not table_name or not column_name:
            continue
        parsed[table_name] = _merge_ordered_strings(parsed.get(table_name, []), [column_name])
    return parsed


async def retrieve_past_corrections(
    query: str,
    pool: asyncpg.Pool,
    settings: Settings,
    limit: int,
) -> list[dict]:
    return await retrieve_similar_corrections(
        query=query,
        pool=pool,
        limit=limit,
        min_similarity=settings.react_past_corrections_similarity,
    )


async def retrieve_join_paths(
    *,
    tables: list[str],
    pool: asyncpg.Pool,
    top_k: int,
    query: str,
) -> list[dict]:
    if len(tables) < 2:
        return []

    normalized_tables = {_normalize_table_name(table) for table in tables if table}
    search_query = f"join path {' '.join(sorted(normalized_tables))} {query}".strip()
    results = await retrieve(
        query=query,
        search_query=search_query,
        top_k=top_k,
        pool=pool,
    )
    paths: list[dict] = []
    for result in results:
        metadata = dict(result.metadata or {})
        if metadata.get("type") != "relation_link":
            continue
        left_table = _normalize_table_name(str(metadata.get("source_table") or ""))
        right_table = _normalize_table_name(str(metadata.get("target_table") or ""))
        if left_table not in normalized_tables or right_table not in normalized_tables:
            continue
        paths.append(
            {
                "left_table": left_table,
                "left_column": "",
                "right_table": right_table,
                "right_column": "",
                "join_type": str(metadata.get("relationship_type") or "INNER"),
                "join_sql": _extract_join_sql(result.content),
                "confidence": metadata.get("confidence"),
            }
        )
    return paths


def _select_state_driven_action(
    *,
    state: dict[str, Any],
    available_actions: list[ReActAction],
    search_query: str,
) -> tuple[ReActAction, str] | None:
    _ensure_state_defaults(state)
    if available_actions == [ReActAction.RETRIEVE_PAST_CORRECTIONS]:
        return ReActAction.RETRIEVE_PAST_CORRECTIONS, search_query

    tables_in_scope = [
        _normalize_table_name(table)
        for table in state.get("tables_in_scope", [])
        if str(table).strip()
    ]
    focus_tables = [
        _normalize_table_name(table)
        for table in state.get("focus_tables", [])
        if str(table).strip()
    ]
    confidence_tables = focus_tables or tables_in_scope
    retrieved_schema = {
        _normalize_table_name(name)
        for name in (state.get("retrieved_schema") or {}).keys()
        if str(name).strip()
    }
    uncovered_tables = [
        table for table in confidence_tables if table not in retrieved_schema
    ]
    if ReActAction.RETRIEVE_SCHEMA_FOR_TABLES in available_actions and (
        not confidence_tables or uncovered_tables
    ):
        target_tables = uncovered_tables or confidence_tables
        action_input = ", ".join(target_tables) if target_tables else search_query
        return (
            ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            action_input or "Retrieve targeted schema for the current question.",
        )

    if (
        ReActAction.RETRIEVE_JOIN_PATHS in available_actions
        and len(focus_tables) > 1
        and not state.get("join_paths")
    ):
        return (
            ReActAction.RETRIEVE_JOIN_PATHS,
            ", ".join(confidence_tables),
        )

    return None


def extract_think_block(raw: str) -> tuple[str, str]:
    think_start = raw.find("<think>")
    think_end = raw.find("</think>")
    if think_start != -1 and think_end != -1 and think_start < think_end:
        thought = raw[think_start + len("<think>") : think_end].strip()
        answer = raw[think_end + len("</think>") :].strip()
        return thought, answer

    return "", raw.strip()


def looks_like_action_payload(raw: str) -> bool:
    if re.search(r'"action"\s*:', raw, flags=re.IGNORECASE):
        return True
    if re.search(r"\b(?:ACTION|NEXT\s+ACTION)\b\s*[:=\-]", raw, flags=re.IGNORECASE):
        return True

    normalized = raw.upper().replace("-", "_").replace(" ", "_")
    return any(action.value in normalized for action in ReActAction)


def combine_search_queries(refined_query: str, base_search_query: str) -> str:
    refined = " ".join(refined_query.split())
    base = " ".join(base_search_query.split())
    if not refined:
        return base
    if not base:
        return refined

    refined_lower = refined.lower()
    base_lower = base.lower()
    if refined_lower == base_lower or refined_lower in base_lower:
        return base
    if base_lower in refined_lower:
        return refined
    return f"{refined}\n{base}"


async def call_reasoning_model(
    prompt: str,
    settings: Settings,
    timeout_budget_seconds: float | None = None,
) -> tuple[str, str, list[SqlWarning]]:
    def _warnings_for_response(error_type: str | None, error_message: str | None) -> list[SqlWarning]:
        code = (
            WarningCode.OLLAMA_TIMEOUT
            if error_type == "timeout"
            else WarningCode.OLLAMA_MALFORMED
            if error_type in {"malformed", "empty"}
            else WarningCode.OLLAMA_UPSTREAM
        )
        detail = error_message or f"returned no text from {client.provider_name}"
        return [
            SqlWarning(
                code=code,
                message=f"Reasoning model {detail}",
            )
        ]

    client = get_model_client(
        settings=settings,
        model=settings.reasoning_model,
        default_timeout=settings.reasoning_timeout,
        role=LLMRole.REASONING.value,
    )
    reasoning_budget = float(settings.reasoning_timeout)
    if timeout_budget_seconds is not None:
        reasoning_budget = min(reasoning_budget, max(0.0, float(timeout_budget_seconds)))
    if reasoning_budget <= 0:
        return "", "", _warnings_for_response("timeout", "timed out before reasoning could start")
    primary_timeout = reasoning_budget if reasoning_budget <= 1.0 else max(1.0, reasoning_budget * 0.7)
    fallback_budget = max(0.0, reasoning_budget - primary_timeout)

    started = time.monotonic()
    response = await client.generate(
        prompt=prompt,
        max_tokens=settings.react_reasoning_max_tokens,
        temperature=settings.reasoning_temperature,
        enable_thinking=True,
        timeout=primary_timeout,
        response_format="json",
    )
    thought = response.thought or ""
    answer = response.text or ""
    if not answer and looks_like_action_payload(thought):
        answer = thought
        thought = ""
    if answer:
        return thought, answer, []

    # Timeout fallback: retry once in compact non-thinking mode, but only within
    # the original reasoning budget.
    if response.error_type == "timeout":
        elapsed = time.monotonic() - started
        fallback_timeout = max(0.0, min(fallback_budget, reasoning_budget - elapsed))
        if fallback_timeout <= 0:
            return "", "", _warnings_for_response(
                response.error_type,
                response.error_message,
            )
        fallback_response = await client.generate(
            prompt=prompt,
            max_tokens=220,
            temperature=0.0,
            enable_thinking=False,
            timeout=fallback_timeout,
            response_format=None,
        )
        fallback_answer = fallback_response.text or ""
        fallback_thought = fallback_response.thought or ""
        if not fallback_answer and looks_like_action_payload(fallback_thought):
            fallback_answer = fallback_thought
            fallback_thought = ""
        if fallback_answer:
            return fallback_thought, fallback_answer, []
        return "", "", _warnings_for_response(
            fallback_response.error_type,
            fallback_response.error_message,
        )

    return "", "", _warnings_for_response(response.error_type, response.error_message)


def parse_action(answer: str) -> tuple[ReActAction, str]:
    def _normalize_token(raw: str) -> str:
        cleaned = raw.strip().strip("`*\"'[](){}.,;:")
        cleaned = cleaned.replace("-", "_").replace(" ", "_")
        cleaned = re.sub(r"[^A-Za-z0-9_]", "", cleaned)
        return cleaned.upper()

    def _resolve_action(raw: str) -> ReActAction | None:
        normalized = _normalize_token(raw)
        if not normalized:
            return None

        exact = {action.value: action for action in ReActAction}
        if normalized in exact:
            return exact[normalized]

        aliases = {
            "RETRIEVE_PAST": ReActAction.RETRIEVE_PAST_CORRECTIONS,
            "LOAD_PAST_CORRECTIONS": ReActAction.RETRIEVE_PAST_CORRECTIONS,
            "RETRIEVE_SCHEMA": ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            "RETRIEVE_CONTEXT": ReActAction.RETRIEVE_MORE_CONTEXT,
            "RETRIEVE_MORE": ReActAction.RETRIEVE_MORE_CONTEXT,
            "FETCH_COLUMNS": ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            "GET_SCHEMA": ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            "FETCH_SCHEMA": ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            "JOIN_PATHS": ReActAction.RETRIEVE_JOIN_PATHS,
            "RETRIEVE_JOINS": ReActAction.RETRIEVE_JOIN_PATHS,
            "SAMPLE_QUERIES": ReActAction.RETRIEVE_SAMPLE_QUERIES,
            "RETRIEVE_EXAMPLES": ReActAction.RETRIEVE_SAMPLE_QUERIES,
            "GENERATE": ReActAction.GENERATE_SQL,
            "WRITE_SQL": ReActAction.GENERATE_SQL,
            "VALIDATE": ReActAction.VALIDATE_AND_RETURN,
            "RETURN_SQL": ReActAction.VALIDATE_AND_RETURN,
            "ASK_CLARIFICATION": ReActAction.REQUEST_CLARIFICATION,
            "REQUEST_CLARIFICATION": ReActAction.REQUEST_CLARIFICATION,
            "CLARIFY": ReActAction.REQUEST_CLARIFICATION,
            "GIVEUP": ReActAction.GIVE_UP,
        }
        if normalized in aliases:
            return aliases[normalized]

        for action in ReActAction:
            if action.value in normalized:
                return action
        for alias, mapped_action in aliases.items():
            if alias in normalized:
                return mapped_action
        return None

    action_text: str | None = None
    action_input = ""

    action_line_pattern = re.compile(
        r"^(?:[-*]\s*)?(?:\*\*)?(?:NEXT\s+)?ACTION(?:\*\*)?\s*[:=\-]\s*(.+)$",
        re.IGNORECASE,
    )
    input_line_pattern = re.compile(
        r"^(?:[-*]\s*)?(?:\*\*)?(?:INPUT|ACTION_INPUT|INSTRUCTION)(?:\*\*)?\s*[:=\-]\s*(.+)$",
        re.IGNORECASE,
    )

    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        action_match = action_line_pattern.match(stripped)
        if action_match and not action_text:
            action_text = action_match.group(1).strip()
            continue

        input_match = input_line_pattern.match(stripped)
        if input_match and not action_input:
            action_input = input_match.group(1).strip()

    if not action_text:
        json_action_match = re.search(r'"action"\s*:\s*"([^"]+)"', answer, flags=re.IGNORECASE)
        if json_action_match:
            action_text = json_action_match.group(1).strip()

    if not action_input:
        json_input_match = re.search(
            r'"(?:input|action_input|instruction)"\s*:\s*"([^"]*)"',
            answer,
            flags=re.IGNORECASE,
        )
        if json_input_match:
            action_input = json_input_match.group(1).strip()

    if not action_text:
        for action in ReActAction:
            pattern = action.value.replace("_", r"[\s_-]*")
            if re.search(rf"\b{pattern}\b", answer, flags=re.IGNORECASE):
                action_text = action.value
                break

    if not action_text:
        natural_language_aliases = [
            (
                ReActAction.RETRIEVE_MORE_CONTEXT,
                r"\b(retrieve|search|find|load)\b.*\b(context|schema\s+group|more)\b",
            ),
            (
                ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
                r"\b(fetch|load|get|inspect)\b.*\b(schema|columns?)\b",
            ),
            (
                ReActAction.RETRIEVE_JOIN_PATHS,
                r"\b(join|relationship|path)\b",
            ),
            (
                ReActAction.RETRIEVE_SAMPLE_QUERIES,
                r"\b(sample|example|previous|similar)\b.*\b(query|pattern|sql)\b",
            ),
            (
                ReActAction.GENERATE_SQL,
                r"\b(generate|write|create|draft)\b.*\bsql\b",
            ),
            (
                ReActAction.VALIDATE_AND_RETURN,
                r"\b(validate|check)\b.*\b(return|sql|query)\b",
            ),
            (
                ReActAction.REQUEST_CLARIFICATION,
                r"\b(ask|request)\b.*\b(clarification|rephrase)\b",
            ),
            (
                ReActAction.GIVE_UP,
                r"\b(give\s*up|cannot|can't|unable|insufficient)\b",
            ),
        ]
        for action, pattern in natural_language_aliases:
            if re.search(pattern, answer, flags=re.IGNORECASE | re.DOTALL):
                action_text = action.value
                break

    if not action_text:
        return ReActAction.GIVE_UP, "Could not parse action"

    parsed_action = _resolve_action(action_text)
    if parsed_action is None:
        return ReActAction.GIVE_UP, "Could not parse action"

    return parsed_action, action_input


def choose_recovery_action_for_parse_failure(
    answer: str,
    state: dict[str, Any],
) -> tuple[ReActAction, str] | None:
    """
    Pick a conservative next action when the planner response is malformed.

    The ReAct loop should not terminate solely because the small planning model
    ignored the requested output shape. Guardrails still validate generated SQL
    before anything is returned or executed.
    """
    if answer.strip() and re.search(
        r"\b(?:ACTION|NEXT\s+ACTION)\b\s*[:=\-]",
        answer,
        flags=re.IGNORECASE,
    ):
        return None

    if state.get("current_sql"):
        if state.get("last_validation_errors"):
            return (
                ReActAction.GENERATE_SQL,
                "Planner output was unparseable; regenerate SQL to fix validation errors.",
            )
        return (
            ReActAction.VALIDATE_AND_RETURN,
            "Planner output was unparseable; validate the current SQL.",
        )

    if not state.get("tables_in_scope"):
        return (
            ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            "Planner output was unparseable; retrieve more schema context.",
        )

    return (
        ReActAction.GENERATE_SQL,
        "Planner output was unparseable; generate SQL from the retrieved context.",
    )


def build_react_prompt(
    query: str,
    context: str,
    tables_in_scope: list[str],
    allowed_columns: dict[str, list[str]],
    history: list[ReActStep],
    current_error: str,
    dialect: str,
    available_actions: list[ReActAction],
    context_confidence_score: float,
    context_confidence_details: dict[str, float],
    learned_instructions: list[str],
    settings: Settings | None = None,
) -> str:
    active_settings = settings or default_settings
    column_lines = [
        f"  {table}: {', '.join(columns)}"
        for table, columns in allowed_columns.items()
    ]
    known_columns = "\n".join(column_lines) if column_lines else "(none)"
    history_lines = [
        (
            f"Step {step.iteration}: Action={step.action.value}, "
            f"Observation={step.observation}"
        )
        for step in history
    ]
    rendered_history = "\n".join(history_lines) if history_lines else "(empty)"
    rendered_actions = "\n".join(
        f"- {action.value}: {_action_description(action)}"
        for action in available_actions
    ) or "- REQUEST_CLARIFICATION: ask the user for clarification"
    rendered_learned = "\n".join(f"- {item}" for item in learned_instructions[:6]) or "(none)"
    governance = ""
    if active_settings.governance_enabled and active_settings.governance_inject_react:
        governance = build_governance_block(
            get_config(active_settings),
            context="react",
        )

    prompt = f"""
You are a SQL planning agent for a {dialect} database.
Your job is to analyse the situation and decide the
next action to take.

AVAILABLE ACTIONS:
{rendered_actions}

USER QUESTION: {query}

RETRIEVED SCHEMA CONTEXT:
{context}

IMPORTANT: If USER-PROVIDED RULES are present above,
follow them strictly. They override your defaults.
Do not ignore table relationships, term mappings,
or filter rules listed there.

TABLES IN SCOPE: {', '.join(tables_in_scope)}

KNOWN COLUMNS:
{known_columns}

HISTORY OF STEPS TAKEN:
{rendered_history}
(empty if first iteration)

CURRENT ERROR (if any): {current_error}
(empty string if no error yet)

DIALECT: {dialect}

CURRENT CONTEXT CONFIDENCE SCORE: {context_confidence_score:.2f}
CONFIDENCE SIGNALS: {json.dumps(context_confidence_details, sort_keys=True)}

LEARNED INSTRUCTIONS / CORRECTIONS:
{rendered_learned}

STRICT RULES:
- Only SELECT or WITH...SELECT statements allowed
- Only use tables listed in TABLES IN SCOPE
- Only use columns listed in KNOWN COLUMNS
- Do not invent column names
- If context is insufficient, use the retrieval action that fills the missing gap
- If context confidence already meets the threshold, prefer GENERATE_SQL
- If SQL passed all checks, use VALIDATE_AND_RETURN
- If retrieval cannot find relevant tables at all after trying, use REQUEST_CLARIFICATION
- If error is unrecoverable, use GIVE_UP

Think carefully about what went wrong and what to do.
Then output EXACTLY:
{{"action":"<one of the available actions above>","input":"<brief instruction for the action>"}}
""".strip()
    if governance:
        return f"{governance}\n\n{prompt}"
    return prompt


async def execute_action(
    action: ReActAction,
    action_input: str,
    query: str,
    pool: asyncpg.Pool,
    settings: Settings,
    state: dict[str, Any],
) -> tuple[str, list[SqlWarning]]:
    _ensure_state_defaults(state)

    if action in {ReActAction.RETRIEVE_MORE_CONTEXT, ReActAction.FETCH_SCHEMA}:
        action = ReActAction.RETRIEVE_SCHEMA_FOR_TABLES

    if action == ReActAction.RETRIEVE_PAST_CORRECTIONS:
        correction_query = action_input or str(state.get("search_query") or query)
        corrections = await retrieve_past_corrections(
            correction_query,
            pool,
            settings,
            settings.react_past_corrections_limit,
        )
        state["past_corrections_checked"] = True
        state["past_corrections"] = corrections
        learned = list(state.get("learned_instructions") or [])
        for correction in corrections:
            correction_tables = [
                _normalize_table_name(table)
                for table in (
                    correction.get("tables_affected")
                    or correction.get("tables_involved")
                    or []
                )
                if str(table).strip()
            ]
            state["tables_in_scope"] = _merge_ordered_strings(
                state["tables_in_scope"],
                correction_tables,
            )
            learned.append(
                (
                    f"Correction for '{correction.get('source_query') or correction_query}': "
                    f"{correction.get('content', '')}"
                ).strip()
            )
        state["learned_instructions"] = _merge_ordered_strings([], learned)
        if corrections:
            correction_lines = []
            for correction in corrections:
                tables = ", ".join(correction.get("tables_affected") or [])
                failed_sql = str(correction.get("failed_sql") or "").strip()
                block = f"- {correction.get('content', '')}".strip()
                if tables:
                    block += f" (tables: {tables})"
                if failed_sql:
                    block += f" | prior failed SQL: {failed_sql[:200]}"
                correction_lines.append(block)
            state["context"] = _merge_context_block(
                state["context"],
                "PAST CORRECTIONS:\n" + "\n".join(correction_lines),
            )
            return f"Retrieved {len(corrections)} past correction(s).", []
        return "No similar past corrections found.", []

    if action == ReActAction.RETRIEVE_SCHEMA_FOR_TABLES:
        refined_query = action_input if action_input else query
        search_query = combine_search_queries(
            refined_query,
            str(state.get("search_query") or query),
        )
        state["query_embedding"] = await _ensure_query_embedding(
            query=search_query,
            state=state,
        )
        result = await retrieve_groups(
            query=refined_query,
            top_k=state["top_k"],
            pool=pool,
            search_query=search_query,
        )
        matched_groups = _result_value(result, "matched_groups")
        tables_in_scope = _result_value(result, "tables_in_scope")
        state["matched_groups"] = _merge_ordered_strings(state["matched_groups"], matched_groups)
        state["tables_in_scope"] = _merge_ordered_strings(state["tables_in_scope"], tables_in_scope)
        state["context"] = _merge_context_block(state["context"], _result_value(result, "context"))

        known_tables = [
            _normalize_table_name(table)
            for table in state["tables_in_scope"]
            if _normalize_table_name(table)
        ]
        explicit_targets = [
            target
            for target in _parse_csv_items(action_input)
            if target in known_tables
        ]
        inferred_focus_tables = _infer_focus_tables(
            str(state.get("search_query") or query),
            known_tables,
        )
        target_tables = explicit_targets or inferred_focus_tables or known_tables
        focus_seed = explicit_targets or inferred_focus_tables
        if not focus_seed and len(known_tables) == 1:
            focus_seed = known_tables
        state["focus_tables"] = _merge_ordered_strings(
            state.get("focus_tables") or [],
            focus_seed,
        )
        column_results = await retrieve_column_catalog(
            query=refined_query,
            tables=target_tables or tables_in_scope,
            top_k=max(
                settings.react_top_k_floor,
                state["top_k"] * settings.react_top_k_multiplier,
            ),
            pool=pool,
            search_query=search_query,
        )
        parsed_columns = _extract_columns_from_results(
            list(_result_value(result, "results")),
            target_tables or tables_in_scope,
        )
        parsed_columns = _merge_allowed_columns(
            parsed_columns,
            _extract_columns_from_column_results(column_results),
        )
        if not parsed_columns and target_tables:
            try:
                parsed_columns = _merge_allowed_columns(
                    parsed_columns,
                    await load_columns_for_tables(
                        tables=target_tables,
                        settings=settings,
                    ),
                )
            except Exception:  # noqa: BLE001
                pass
        state["retrieved_schema"] = _merge_allowed_columns(
            state.get("retrieved_schema") or {},
            parsed_columns,
        )
        state["allowed_columns"] = _merge_allowed_columns(
            state.get("allowed_columns") or {},
            parsed_columns,
        )
        state["columns_refreshed_tables"].update(parsed_columns)
        state["retrieved_tables"].update(target_tables or {
            _normalize_table_name(table)
            for table in tables_in_scope
        })
        if parsed_columns:
            state["context"] = _merge_context_block(
                state["context"],
                "RETRIEVED COLUMNS:\n"
                + "\n".join(
                    f"- {table}: {', '.join(columns)}"
                    for table, columns in parsed_columns.items()
                ),
            )
        observation = (
            f"Retrieved schema for {len(target_tables or tables_in_scope)} table(s): "
            f"{', '.join(target_tables or tables_in_scope)}. "
            f"tables_in_scope={', '.join(state['tables_in_scope']) or '(none)'}. "
            f"Focus tables: {', '.join(state.get('focus_tables') or []) or '(none)'}. "
            f"Matched groups: {', '.join(state['matched_groups']) or '(none)'}. "
            f"Columns refreshed via column-level retrieval for {len(parsed_columns)} table(s)."
        )
        return observation, []

    if action == ReActAction.RETRIEVE_JOIN_PATHS:
        table_names = [
            _normalize_table_name(table)
            for table in (_parse_csv_items(action_input) or state.get("tables_in_scope", []))
            if str(table).strip()
        ]
        join_paths = await retrieve_join_paths(
            tables=table_names,
            pool=pool,
            top_k=settings.react_relation_retrieval_top_k,
            query=query,
        )
        state["join_paths"] = join_paths
        if join_paths:
            join_lines = []
            for join_path in join_paths:
                join_sql = str(join_path.get("join_sql") or "").strip()
                if join_sql:
                    join_lines.append(f"- {join_sql}")
                else:
                    join_lines.append(
                        "- "
                        f"{join_path.get('left_table')}.{join_path.get('left_column')} = "
                        f"{join_path.get('right_table')}.{join_path.get('right_column')}"
                    )
            state["context"] = _merge_context_block(
                state["context"],
                "JOIN PATHS:\n" + "\n".join(join_lines),
            )
        return f"Retrieved {len(join_paths)} join path(s).", []

    if action == ReActAction.RETRIEVE_SAMPLE_QUERIES:
        patterns = await pattern_store.get_relevant_patterns(
            query=action_input or query,
            tables_in_scope=list(state.get("tables_in_scope") or []),
            pool=pool,
            limit=settings.react_sample_query_limit,
            min_use_count=settings.min_pattern_use_count,
        )
        state["sample_queries"] = patterns
        if patterns:
            state["context"] = _merge_context_block(
                state["context"],
                "SAMPLE QUERIES:\n" + pattern_store.format_patterns_for_prompt(patterns),
            )
            learned = list(state.get("learned_instructions") or [])
            learned.extend(
                f"Sample query: {pattern.get('query_text', '')}"
                for pattern in patterns
            )
            state["learned_instructions"] = _merge_ordered_strings([], learned)
        return f"Retrieved {len(patterns)} sample querie(s).", []

    if action == ReActAction.GENERATE_SQL:
        generation_count = state["sql_generation_count"]
        generation_tables = _generation_tables_in_scope(state)
        generation_columns = _generation_allowed_columns(state)
        if state["current_sql"] and state["last_validation_errors"]:
            prompt = build_refinement_prompt(
                query=query,
                context=state["context"],
                tables_in_scope=generation_tables,
                dialect=settings.sql_dialect,
                previous_sql=state["current_sql"],
                validation_errors=state["last_validation_errors"],
                attempt=generation_count,
                allowed_columns=generation_columns,
                planner_instruction=action_input,
                settings=settings,
            )
        else:
            prompt = build_sql_prompt(
                query=query,
                context=state["context"],
                tables_in_scope=generation_tables,
                allowed_columns=generation_columns,
                dialect=settings.sql_dialect,
                planner_instruction=action_input,
                settings=settings,
            )
        raw, warnings = await call_ollama(
            prompt=prompt,
            settings=settings,
            timeout=_remaining_stage_budget_seconds(state),
        )
        if warnings:
            return "SQL generation failed", warnings

        sql = narrow_select_star(
            extract_sql(raw or ""),
            generation_columns,
            query,
        )
        state["current_sql"] = sql
        state["sql_generation_count"] = generation_count + 1
        state["last_validation_errors"] = []
        if len(sql) > 200:
            observation = f"Generated SQL: {sql[:200]}..."
        else:
            observation = f"Generated: {sql}"
        return observation, []

    if action == ReActAction.VALIDATE_AND_RETURN:
        sql = state.get("current_sql") or ""
        if not sql:
            return "No SQL to validate.", [
                SqlWarning(
                    code=WarningCode.SQL_EMPTY,
                    message="No SQL generated yet",
                )
            ]

        warnings: list[SqlWarning] = []
        safety_warnings = validate_sql_safety(sql, settings.sql_dialect)
        warnings.extend(safety_warnings)

        if not safety_warnings:
            generation_tables = _generation_tables_in_scope(state)
            generation_columns = _generation_allowed_columns(state)
            tables_used, table_warnings = validate_tables_used(
                sql,
                generation_tables,
            )
            state["tables_used"] = tables_used
            warnings.extend(table_warnings)
            warnings.extend(validate_columns_used(sql, generation_columns))

            static_blocking = _blocking_warnings(warnings)
            if not static_blocking:
                warnings.extend(await run_explain(sql, settings))

        blocking_warnings = _blocking_warnings(warnings)
        info_warnings = [
            warning
            for warning in warnings
            if warning.code == WarningCode.MYSQL_EXPLAIN_UNAVAILABLE
        ]

        if not blocking_warnings:
            state["last_validation_errors"] = []
            observation = "PASSED: SQL is valid and safe."
            return observation, info_warnings

        state["last_validation_errors"] = blocking_warnings
        error_summary = "; ".join(warning.message for warning in blocking_warnings)
        observation = f"FAILED: {error_summary}"
        return observation, blocking_warnings

    if action in {ReActAction.REQUEST_CLARIFICATION, ReActAction.ASK_CLARIFICATION}:
        return "Agent requested clarification.", []

    return f"Agent gave up: {action_input}", []


async def build_clarification(
    query: str,
    failure_reason: str,
    all_warnings: list[SqlWarning],
    react_trace: ReactTrace,
    settings: Settings,
    tables_in_scope: list[str] | None = None,
    stage_latencies_ms: dict[str, int] | None = None,
) -> GenerateSqlClarification:
    del all_warnings
    rendered_tables = ", ".join(tables_in_scope or []) or "(unknown)"
    prompt = f"""
A user asked a database question but SQL generation failed.

User question: "{query}"
Failure reason: "{failure_reason}"
Candidate tables: "{rendered_tables}"

Your job: ask ONE clarifying question and provide
2-3 refined query suggestions that would work better.

Be specific. Use database terms where helpful.
Each suggestion should name a likely table or column when possible.

Output EXACTLY this format - no other text:
QUESTION: <one clear clarifying question>
SUGGESTION_1: <refined query>
SUGGESTION_2: <refined query>
SUGGESTION_3: <optional third suggestion>
""".strip()
    fallback_question = (
        f"I couldn't generate valid SQL for '{query}'. "
        "Could you rephrase or add more detail?"
    )
    fallback_suggestions = _build_schema_derived_suggestions(tables_in_scope or [])

    question = fallback_question
    suggestions = fallback_suggestions
    try:
        client = get_model_client(
            settings=settings,
            model=settings.reasoning_model,
            default_timeout=settings.reasoning_timeout,
            role=LLMRole.REASONING.value,
        )
        response = await client.generate(
            prompt=prompt,
            max_tokens=settings.react_planner_max_tokens,
            temperature=0.3,
            enable_thinking=False,
            timeout=settings.reasoning_timeout,
        )
        parsed_question, parsed_suggestions = _parse_clarification_response(response.text)
        if parsed_question and len(parsed_suggestions) >= 2:
            question = parsed_question
            suggestions = parsed_suggestions[:3]
    except Exception:  # noqa: BLE001
        pass

    return GenerateSqlClarification(
        question=question,
        suggestions=suggestions[:3],
        original_query=query,
        failure_reason=failure_reason,
        react_trace=react_trace,
        stage_latencies_ms=stage_latencies_ms,
    )


async def run(
    query: str,
    pool: asyncpg.Pool,
    settings: Settings,
    top_k: int | None = None,
    trace_callback: TraceCallback | None = None,
) -> GenerateSqlResponse:
    stage_latencies_ms: dict[str, int] = {}

    rewrite_started = time.monotonic()
    await _emit_trace(
        trace_callback,
        stage="query_rewrite",
        status="started",
        message="Rewriting the prompt for schema retrieval.",
    )
    search_query = await query_rewriter.rewrite_search_query(query, pool, settings)
    await _emit_trace(
        trace_callback,
        stage="query_rewrite",
        status="completed",
        message="Prompt rewrite completed.",
        duration_ms=int((time.monotonic() - rewrite_started) * 1000),
        details={
            "used_rewrite": search_query != query,
            "search_query": search_query,
        },
    )

    state: dict[str, Any] = {
        "context": "",
        "tables_in_scope": [],
        "matched_groups": [],
        "allowed_columns": {},
        "current_sql": None,
        "last_validation_errors": [],
        "sql_generation_count": 0,
        "tables_used": [],
        "top_k": top_k or settings.top_k,
        "search_query": search_query,
        "actions_taken": [],
        "retrieved_tables": set(),
        "retrieved_schema": {},
        "past_corrections": [],
        "past_corrections_checked": not _should_check_past_corrections(search_query, settings),
        "learned_instructions": [],
        "sample_queries": [],
        "join_paths": [],
        "context_confidence_score": 0.0,
        "context_confidence_details": {},
        "columns_refreshed_tables": set(),
        "generation_tables_in_scope": [],
        "query_embedding": None,
        "focus_tables": [],
        "deadline_monotonic": time.monotonic() + max(0.001, float(settings.sql_generation_timeout)),
    }

    steps: list[ReActStep] = []
    all_warnings: list[SqlWarning] = []
    current_error = ""
    last_completed_action = ReActAction.GIVE_UP

    bootstrap_started = time.monotonic()
    await _emit_trace(
        trace_callback,
        stage="schema_retrieval",
        status="started",
        message="Bootstrap schema retrieval started before the ReAct loop.",
        details={"phase": "bootstrap", "top_k": top_k or settings.top_k},
    )
    bootstrap_observation, bootstrap_warnings = await execute_action(
        action=ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
        action_input=search_query,
        query=query,
        pool=pool,
        settings=settings,
        state=state,
    )
    stage_latencies_ms["schema_retrieval"] = int((time.monotonic() - bootstrap_started) * 1000)
    await _emit_trace(
        trace_callback,
        stage="schema_retrieval",
        status="completed" if not bootstrap_warnings else "warning",
        message="Bootstrap schema retrieval finished before the ReAct loop.",
        duration_ms=stage_latencies_ms["schema_retrieval"],
        warning_codes=[warning.code.value for warning in bootstrap_warnings],
        details={
            "phase": "bootstrap",
            "observation": bootstrap_observation,
            "tables_in_scope": state.get("tables_in_scope", []),
            "focus_tables": state.get("focus_tables", []),
        },
    )
    if bootstrap_warnings:
        all_warnings.extend(bootstrap_warnings)

    max_iterations = settings.react_max_iterations
    planner_iterations = 0
    iteration = 0
    while planner_iterations < max_iterations or not state.get("past_corrections_checked"):
        if (_remaining_stage_budget_seconds(state) or 0.0) <= 0:
            trace = ReactTrace(
                steps=steps,
                total_iterations=len(steps),
                final_action=last_completed_action if steps else ReActAction.GIVE_UP,
            )
            return GenerateSqlRejected(
                warnings=[
                    *all_warnings,
                    SqlWarning(
                        code=WarningCode.REQUEST_TIMEOUT,
                        message=(
                            "SQL generation exceeded the service time budget "
                            f"of {settings.sql_generation_timeout}s."
                        ),
                    ),
                ],
                attempt_count=max(1, planner_iterations) if planner_iterations else 0,
                react_trace=trace,
                stage_latencies_ms=stage_latencies_ms,
            )
        iteration += 1
        state["context_confidence_score"], state["context_confidence_details"] = (
            compute_context_confidence_score(
                state=state,
                iteration_count=max(1, planner_iterations + 1),
                settings=settings,
            )
        )
        available_actions = _build_available_actions(state, iteration, settings)

        step_started = time.monotonic()
        await _emit_trace(
            trace_callback,
            stage="react_iteration",
            status="started",
            message=f"Planning iteration {iteration} started.",
            details={
                "iteration": iteration,
                "available_actions": [action.value for action in available_actions],
                "context_confidence_score": state["context_confidence_score"],
                "context_confidence_details": state["context_confidence_details"],
            },
        )
        thought = ""
        action_input = ""
        state_driven_action = _select_state_driven_action(
            state=state,
            available_actions=available_actions,
            search_query=search_query,
        )
        if state_driven_action is not None:
            action, action_input = state_driven_action
        elif (
            state["context_confidence_score"] >= settings.react_confidence_threshold
            and state.get("tables_in_scope")
        ):
            action = ReActAction.GENERATE_SQL
            action_input = "Context confidence threshold reached; generate SQL with available context."
        else:
            prompt = build_react_prompt(
                query=query,
                context=state["context"],
                tables_in_scope=state["tables_in_scope"],
                allowed_columns=state["allowed_columns"],
                history=steps,
                current_error=current_error,
                dialect=settings.sql_dialect,
                available_actions=available_actions,
                context_confidence_score=state["context_confidence_score"],
                context_confidence_details=state["context_confidence_details"],
                learned_instructions=list(state.get("learned_instructions") or []),
                settings=settings,
            )
            thought, answer, reason_warnings = await call_reasoning_model(
                prompt,
                settings,
                timeout_budget_seconds=_remaining_stage_budget_seconds(state),
            )
            if reason_warnings:
                all_warnings.extend(reason_warnings)
                duration = int((time.monotonic() - step_started) * 1000)
                await _emit_trace(
                    trace_callback,
                    stage="react_iteration",
                    status="failed",
                    message=f"Planning iteration {iteration} failed during model reasoning.",
                    duration_ms=duration,
                    warning_codes=[warning.code.value for warning in reason_warnings],
                    error_source="generation_transport",
                    details={"iteration": iteration},
                )
                trace = ReactTrace(
                    steps=steps,
                    total_iterations=len(steps),
                    final_action=last_completed_action if steps else ReActAction.GIVE_UP,
                )
                return GenerateSqlRejected(
                    warnings=all_warnings,
                    attempt_count=len(steps),
                    react_trace=trace,
                    stage_latencies_ms=stage_latencies_ms,
                )

            action, action_input = parse_action(answer)
            if action == ReActAction.GIVE_UP and action_input == "Could not parse action":
                recovery_action = choose_recovery_action_for_parse_failure(answer, state)
                if recovery_action is not None:
                    action, action_input = recovery_action
            if action not in available_actions:
                action = available_actions[0]
                action_input = action_input or "Planner selected an unavailable action; using the highest-priority available action instead."

        guarded_action, guard_reason = apply_iteration_memory_guard(
            action=action,
            action_input=action_input,
            state=state,
        )
        if guard_reason:
            action = guarded_action
            action_input = action_input or guard_reason

        if action == ReActAction.VALIDATE_AND_RETURN and state["last_validation_errors"]:
            action = ReActAction.GENERATE_SQL
            action_input = (
                "Previous SQL failed validation; regenerate SQL to fix validation errors."
            )

        if action == ReActAction.GENERATE_SQL:
            tables_before_focus = [
                _normalize_table_name(table)
                for table in state.get("tables_in_scope", [])
                if str(table).strip()
            ]
            focused_tables = list(tables_before_focus)
            column_hits_used = 0
            query_embedding = await _ensure_query_embedding(
                query=str(state.get("search_query") or query),
                state=state,
            )
            if query_embedding is not None and tables_before_focus:
                focused_tables, column_hits_used = await _focused_tables_for_generation(
                    tables_in_scope=tables_before_focus,
                    query_embedding=query_embedding,
                    vector_store=PgVectorStore(pool),
                    max_tables=settings.sql_generation_max_tables,
                )
            elif tables_before_focus:
                focused_tables = tables_before_focus[: settings.sql_generation_max_tables]
            state["generation_tables_in_scope"] = focused_tables
            await _emit_trace(
                trace_callback,
                stage="sql_context_focus",
                status="completed",
                message="Focused SQL generation context to the most relevant tables.",
                details={
                    "tables_before_focus": len(tables_before_focus),
                    "tables_after_focus": focused_tables,
                    "column_hits_used": column_hits_used,
                },
            )
            if (
                len(tables_before_focus) > settings.sql_generation_max_tables
                and not state.get("columns_refreshed_tables")
                and focused_tables
            ):
                action = ReActAction.RETRIEVE_SCHEMA_FOR_TABLES
                action_input = ",".join(focused_tables)
        else:
            state["generation_tables_in_scope"] = []

        parallel_iteration_one_retrieval = (
            iteration == 1
            and action == ReActAction.RETRIEVE_PAST_CORRECTIONS
            and not state.get("past_corrections_checked")
        )
        parallel_schema_input = ""

        if parallel_iteration_one_retrieval or action in {
            ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            ReActAction.RETRIEVE_JOIN_PATHS,
            ReActAction.RETRIEVE_MORE_CONTEXT,
            ReActAction.FETCH_SCHEMA,
        }:
            schema_started = time.monotonic()
            await _emit_trace(
                trace_callback,
                stage="schema_retrieval",
                status="started",
                message=f"Iteration {iteration} started schema retrieval.",
                details={
                    "iteration": iteration,
                    "action": (
                        ReActAction.RETRIEVE_SCHEMA_FOR_TABLES.value
                        if parallel_iteration_one_retrieval
                        else action.value
                    ),
                    "top_k": top_k or settings.top_k,
                    "parallel_with": (
                        ReActAction.RETRIEVE_PAST_CORRECTIONS.value
                        if parallel_iteration_one_retrieval
                        else None
                    ),
                },
            )
        else:
            schema_started = None

        if parallel_iteration_one_retrieval:
            observation, action_warnings = await _execute_iteration_one_parallel_retrieval(
                action_input=action_input,
                query=query,
                pool=pool,
                settings=settings,
                state=state,
            )
        else:
            observation, action_warnings = await execute_action(
                action=action,
                action_input=action_input,
                query=query,
                pool=pool,
                settings=settings,
                state=state,
            )
        if schema_started is not None:
            schema_duration = int((time.monotonic() - schema_started) * 1000)
            stage_latencies_ms["schema_retrieval"] = schema_duration
            await _emit_trace(
                trace_callback,
                stage="schema_retrieval",
                status="completed",
                message=f"Iteration {iteration} completed schema retrieval.",
                duration_ms=schema_duration,
                details={
                    "iteration": iteration,
                    "action": (
                        ReActAction.RETRIEVE_SCHEMA_FOR_TABLES.value
                        if parallel_iteration_one_retrieval
                        else action.value
                    ),
                    "tables_in_scope": state.get("tables_in_scope", []),
                    "retrieved_tables": sorted(state.get("retrieved_tables", set())),
                    "context_confidence_score": state.get("context_confidence_score"),
                    "parallel_with": (
                        ReActAction.RETRIEVE_PAST_CORRECTIONS.value
                        if parallel_iteration_one_retrieval
                        else None
                    ),
                },
            )
        completed_action = (
            ReActAction.RETRIEVE_SCHEMA_FOR_TABLES
            if parallel_iteration_one_retrieval
            else action
        )

        if parallel_iteration_one_retrieval:
            state["actions_taken"].append(
                (
                    ReActAction.RETRIEVE_PAST_CORRECTIONS.value,
                    _action_target(
                        ReActAction.RETRIEVE_PAST_CORRECTIONS,
                        action_input,
                        state,
                    ),
                )
            )
            state["actions_taken"].append(
                (
                    ReActAction.RETRIEVE_SCHEMA_FOR_TABLES.value,
                    _action_target(
                        ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
                        parallel_schema_input,
                        state,
                    ),
                )
            )
        else:
            state["actions_taken"].append((action.value, _action_target(action, action_input, state)))

        blocking_action_warnings = _blocking_warnings(action_warnings)
        if action == ReActAction.GENERATE_SQL and not blocking_action_warnings:
            validation_observation, validation_warnings = await execute_action(
                action=ReActAction.VALIDATE_AND_RETURN,
                action_input="Auto-validate SQL generated in this iteration.",
                query=query,
                pool=pool,
                settings=settings,
                state=state,
            )
            observation = f"{observation}\nAuto-validation: {validation_observation}"
            action_warnings = [*action_warnings, *validation_warnings]
            blocking_action_warnings = _blocking_warnings(action_warnings)
            completed_action = ReActAction.VALIDATE_AND_RETURN

        step_duration_ms = int((time.monotonic() - step_started) * 1000)
        warning_codes = [warning.code.value for warning in action_warnings]
        await _emit_trace(
            trace_callback,
            stage="react_iteration",
            status="completed" if not warning_codes else "warning",
            message=f"Planning iteration {iteration} selected {action.value}.",
            duration_ms=step_duration_ms,
            warning_codes=warning_codes,
            reasoning_summary=summarize_text(thought),
            details={
                "iteration": iteration,
                "action": action.value,
                "action_input": action_input,
                "observation": observation[:1000],
                "completed_action": completed_action.value,
                "sql_preview": (state.get("current_sql") or "")[:500],
                "tables_in_scope": state.get("tables_in_scope", []),
                "tables_used": state.get("tables_used", []),
                "matched_groups": state.get("matched_groups", []),
                "past_corrections_count": len(state.get("past_corrections") or []),
                "context_confidence_score": state.get("context_confidence_score"),
                "context_confidence_details": state.get("context_confidence_details", {}),
            },
            input_summary={"iteration": iteration, "action_input": summarize_text(action_input)},
            output_summary={"completed_action": completed_action.value},
        )
        steps.append(
            ReActStep(
                iteration=iteration,
                thought=thought,
                action=action,
                action_input=action_input,
                observation=observation,
                duration_ms=step_duration_ms,
            )
        )
        last_completed_action = completed_action
        if action not in {
            ReActAction.RETRIEVE_PAST_CORRECTIONS,
            ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            ReActAction.RETRIEVE_JOIN_PATHS,
            ReActAction.RETRIEVE_SAMPLE_QUERIES,
            ReActAction.RETRIEVE_MORE_CONTEXT,
            ReActAction.FETCH_SCHEMA,
        }:
            planner_iterations += 1

        if _is_transport_failure(action_warnings):
            all_warnings.extend(action_warnings)
            await _emit_trace(
                trace_callback,
                stage="sql_generation",
                status="failed",
                message="SQL planning stopped because the generation transport failed.",
                warning_codes=[warning.code.value for warning in all_warnings],
                error_source="generation_transport",
            )
            trace = ReactTrace(
                steps=steps,
                total_iterations=iteration,
                final_action=completed_action,
            )
            return GenerateSqlRejected(
                warnings=all_warnings,
                attempt_count=max(1, planner_iterations),
                react_trace=trace,
                stage_latencies_ms=stage_latencies_ms,
            )

        if action in {
            ReActAction.GIVE_UP,
            ReActAction.ASK_CLARIFICATION,
            ReActAction.REQUEST_CLARIFICATION,
        }:
            trace = ReactTrace(
                steps=steps,
                total_iterations=iteration,
                final_action=action,
            )
            asyncio.create_task(
                record_instruction_outcome(
                    tables_used=state.get("tables_in_scope", []),
                    success=False,
                    pool=pool,
                )
            )
            await _emit_trace(
                trace_callback,
                stage="sql_generation",
                status="needs_context"
                if action in {ReActAction.ASK_CLARIFICATION, ReActAction.REQUEST_CLARIFICATION}
                else "failed",
                message=action_input or "Could not generate valid SQL.",
                details={
                    "action": action.value,
                    "tables_in_scope": state.get("tables_in_scope", []),
                    "matched_groups": state.get("matched_groups", []),
                    "context_confidence_score": state.get("context_confidence_score"),
                },
            )
            return await build_clarification(
                query=query,
                failure_reason=action_input or "Could not generate valid SQL",
                all_warnings=all_warnings,
                react_trace=trace,
                settings=settings,
                tables_in_scope=state.get("tables_in_scope", []),
                stage_latencies_ms=stage_latencies_ms,
            )

        if blocking_action_warnings:
            current_error = "; ".join(
                warning.message for warning in blocking_action_warnings
            )
            all_warnings.extend(action_warnings)

        if (
            completed_action == ReActAction.VALIDATE_AND_RETURN
            and not blocking_action_warnings
        ):
            info_warnings = [
                warning
                for warning in action_warnings
                if warning.code == WarningCode.MYSQL_EXPLAIN_UNAVAILABLE
            ]
            trace = ReactTrace(
                steps=steps,
                total_iterations=iteration,
                final_action=completed_action,
            )
            asyncio.create_task(
                record_instruction_outcome(
                    tables_used=state["tables_used"],
                    success=True,
                    pool=pool,
                )
            )
            await _emit_trace(
                trace_callback,
                stage="sql_generation",
                status="completed",
                message="SQL generated and validated.",
                details={
                    "tables_used": state["tables_used"],
                    "matched_groups": state["matched_groups"],
                    "sql_preview": (state.get("current_sql") or "")[:500],
                    "attempt_count": iteration,
                    "context_confidence_score": state.get("context_confidence_score"),
                },
            )
            return GenerateSqlSuccess(
                sql=state["current_sql"],
                warnings=info_warnings,
                tables_used=state["tables_used"],
                matched_groups=state["matched_groups"],
                attempt_count=max(1, state["sql_generation_count"]),
                react_trace=trace,
                stage_latencies_ms=stage_latencies_ms,
            )

    if planner_iterations >= max_iterations and (
        not steps
        or steps[-1].action not in {
            ReActAction.GIVE_UP,
            ReActAction.ASK_CLARIFICATION,
            ReActAction.REQUEST_CLARIFICATION,
        }
    ):
        all_warnings.append(
            SqlWarning(
                code=WarningCode.MAX_RETRIES_EXCEEDED,
                message=(
                    "ReAct loop exhausted after "
                    f"{planner_iterations} planner iteration(s) without valid SQL."
                ),
            )
        )
    trace = ReactTrace(
        steps=steps,
        total_iterations=len(steps),
        final_action=last_completed_action if steps else ReActAction.GIVE_UP,
    )
    codes = [warning.code.value for warning in all_warnings]
    failure_reason = "; ".join(codes) if codes else "Could not generate valid SQL"
    asyncio.create_task(
        record_instruction_outcome(
            tables_used=state.get("tables_in_scope", []),
            success=False,
            pool=pool,
        )
    )
    await _emit_trace(
        trace_callback,
        stage="sql_generation",
        status="failed",
        message=failure_reason,
        warning_codes=codes,
        error_source="sql_generation",
        details={
            "attempt_count": len(steps),
            "tables_in_scope": state.get("tables_in_scope", []),
            "matched_groups": state.get("matched_groups", []),
            "context_confidence_score": state.get("context_confidence_score"),
            "context_confidence_details": state.get("context_confidence_details", {}),
        },
    )
    return await build_clarification(
        query=query,
        failure_reason=failure_reason,
        all_warnings=all_warnings,
        react_trace=trace,
        settings=settings,
        tables_in_scope=state.get("tables_in_scope", []),
        stage_latencies_ms=stage_latencies_ms,
    )


def _blocking_warnings(warnings: list[SqlWarning]) -> list[SqlWarning]:
    return [
        warning
        for warning in warnings
        if warning.code != WarningCode.MYSQL_EXPLAIN_UNAVAILABLE
    ]


def _is_transport_failure(warnings: list[SqlWarning]) -> bool:
    transport_codes = {
        WarningCode.OLLAMA_TIMEOUT,
        WarningCode.OLLAMA_UPSTREAM,
        WarningCode.OLLAMA_MALFORMED,
    }
    return bool(warnings) and all(warning.code in transport_codes for warning in warnings)


def _parse_clarification_response(text: str) -> tuple[str, list[str]]:
    question = ""
    suggestions: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        question_match = re.match(r"QUESTION\s*:\s*(.+)", stripped, flags=re.IGNORECASE)
        if question_match:
            question = question_match.group(1).strip()
            continue
        suggestion_match = re.match(
            r"SUGGESTION_\d+\s*:\s*(.+)",
            stripped,
            flags=re.IGNORECASE,
        )
        if suggestion_match:
            suggestion = suggestion_match.group(1).strip()
            if suggestion:
                suggestions.append(suggestion)
    return question, suggestions


def _result_value(result: Any, field: str) -> Any:
    if isinstance(result, dict):
        return result[field]
    return getattr(result, field)


async def _execute_iteration_one_parallel_retrieval(
    *,
    action_input: str,
    query: str,
    pool: asyncpg.Pool,
    settings: Settings,
    state: dict[str, Any],
) -> tuple[str, list[SqlWarning]]:
    """
    Bootstrap iteration 1 by loading correction memory and initial schema
    context concurrently. Both actions mutate the same planner state before
    iteration 2 is allowed to plan.
    """
    past_result, schema_result = await asyncio.gather(
        execute_action(
            action=ReActAction.RETRIEVE_PAST_CORRECTIONS,
            action_input=action_input,
            query=query,
            pool=pool,
            settings=settings,
            state=state,
        ),
        execute_action(
            action=ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            action_input="",
            query=query,
            pool=pool,
            settings=settings,
            state=state,
        ),
    )
    past_observation, past_warnings = past_result
    schema_observation, schema_warnings = schema_result
    observation = (
        f"Past corrections: {past_observation}\n"
        f"Schema retrieval: {schema_observation}"
    )
    return observation, [*past_warnings, *schema_warnings]
