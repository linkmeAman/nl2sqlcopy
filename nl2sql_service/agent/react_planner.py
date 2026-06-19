from __future__ import annotations
from . import react_executor

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable

import asyncpg

from nl2sql_service.db.column_loader import load_columns_for_tables
from nl2sql_service.storage import pattern_store
from nl2sql_service.generation import query_rewriter
from nl2sql_service.core.config import Settings, settings as default_settings
from nl2sql_service.storage.instruction_store import (
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
from nl2sql_service.core.roles import LLMRole
from nl2sql_service.agent.react_parser import extract_think_block, looks_like_action_payload, parse_action
from nl2sql_service.core.rulebook import build_governance_block, get_config
from nl2sql_service.rag.retrieve import retrieve, retrieve_column_catalog, retrieve_groups
from nl2sql_service.generation.sql_generator import (
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


def _action_target(action: ReActAction, action_input: str, state: dict[str, Any]) -> str:
    from .react_executor import _normalize_table_name, _parse_csv_items

    if action in {
        ReActAction.RETRIEVE_MORE_CONTEXT,
        ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
        ReActAction.FETCH_SCHEMA,
    }:
        targets = react_executor._parse_csv_items(action_input)
        if not targets:
            targets = [react_executor._normalize_table_name(table) for table in state.get("tables_in_scope", [])]
        return ",".join(targets)
    if action == ReActAction.RETRIEVE_JOIN_PATHS:
        targets = sorted(react_executor._normalize_table_name(table) for table in state.get("tables_in_scope", []) if table)
        return ",".join(targets)
    if action in {
        ReActAction.RETRIEVE_SAMPLE_QUERIES,
        ReActAction.RETRIEVE_PAST_CORRECTIONS,
    }:
        return " ".join(str(action_input or state.get("search_query") or "").split()).lower()
    return action.value


def _build_available_actions(
    state: dict[str, Any],
    iteration: int,
    settings: Settings,
) -> list[ReActAction]:
    react_executor._ensure_state_defaults(state)
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
    retrieved_keys = {react_executor._normalize_table_name(name) for name in retrieved_schema.keys()}
    uncovered_tables = [
        table for table in confidence_tables
        if react_executor._normalize_table_name(table) not in retrieved_keys
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


async def call_reasoning_model(
    prompt: str,
    settings: Settings,
    timeout_budget_seconds: float | None = None,
) -> tuple[str, str, list[SqlWarning]]:
    print("REAL REASONING MODEL CALLED!!!")
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


