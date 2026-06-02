from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable

import asyncpg

from nl2sql_service import query_rewriter
from nl2sql_service.column_loader import load_columns_for_tables
from nl2sql_service.config import Settings, settings as default_settings
from nl2sql_service.instruction_store import record_instruction_outcome
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
from nl2sql_service.rulebook import build_governance_block, get_config
from nl2sql_service.retrieve import retrieve_groups
from nl2sql_service.sql_generator import (
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
        role="reasoning",
    )
    response = await client.generate(
        prompt=prompt,
        max_tokens=800,
        temperature=settings.reasoning_temperature,
        enable_thinking=True,
        timeout=settings.reasoning_timeout,
        response_format="json",
    )
    thought = response.thought or ""
    answer = response.text or ""
    if not answer and looks_like_action_payload(thought):
        answer = thought
        thought = ""
    if answer:
        return thought, answer, []

    # Timeout fallback: retry once in compact non-thinking mode to reduce latency.
    if response.error_type == "timeout":
        fallback_timeout = min(max(10, settings.reasoning_timeout // 2), settings.reasoning_timeout)
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
            "RETRIEVE_CONTEXT": ReActAction.RETRIEVE_MORE_CONTEXT,
            "RETRIEVE_MORE": ReActAction.RETRIEVE_MORE_CONTEXT,
            "FETCH_COLUMNS": ReActAction.FETCH_SCHEMA,
            "GET_SCHEMA": ReActAction.FETCH_SCHEMA,
            "GENERATE": ReActAction.GENERATE_SQL,
            "WRITE_SQL": ReActAction.GENERATE_SQL,
            "VALIDATE": ReActAction.VALIDATE_AND_RETURN,
            "RETURN_SQL": ReActAction.VALIDATE_AND_RETURN,
            "ASK_CLARIFICATION": ReActAction.ASK_CLARIFICATION,
            "CLARIFY": ReActAction.ASK_CLARIFICATION,
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
                ReActAction.FETCH_SCHEMA,
                r"\b(fetch|load|get|inspect)\b.*\b(schema|columns?)\b",
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
                ReActAction.ASK_CLARIFICATION,
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
            ReActAction.RETRIEVE_MORE_CONTEXT,
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
- RETRIEVE_MORE_CONTEXT: re-search schema groups with
  different search terms
- FETCH_SCHEMA: load live column list from MySQL for
  a specific table
- GENERATE_SQL: generate a {dialect} SELECT statement
  using deepseek-coder
- VALIDATE_AND_RETURN: current SQL passed validation,
  return it as the answer
- ASK_CLARIFICATION: use this when you have tried to
  retrieve context and cannot find relevant tables at
  all - the user needs to rephrase their question
- GIVE_UP: cannot generate valid SQL, explain why

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

STRICT RULES:
- Only SELECT or WITH...SELECT statements allowed
- Only use tables listed in TABLES IN SCOPE
- Only use columns listed in KNOWN COLUMNS
- Do not invent column names
- If context is insufficient, use RETRIEVE_MORE_CONTEXT
- If columns are missing, use FETCH_SCHEMA
- If everything looks good, use GENERATE_SQL
- If SQL passed all checks, use VALIDATE_AND_RETURN
- If retrieval cannot find relevant tables at all after trying, use ASK_CLARIFICATION
- If error is unrecoverable, use GIVE_UP

Think carefully about what went wrong and what to do.
Then output EXACTLY:
{{"action":"<one of the six actions above>","input":"<brief instruction for the action>"}}
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
    if action == ReActAction.RETRIEVE_MORE_CONTEXT:
        refined_query = action_input if action_input else query
        search_query = combine_search_queries(
            refined_query,
            str(state.get("search_query") or query),
        )
        result = await retrieve_groups(
            query=refined_query,
            top_k=state["top_k"],
            pool=pool,
            search_query=search_query,
        )
        state["context"] = _result_value(result, "context")
        state["tables_in_scope"] = _result_value(result, "tables_in_scope")
        state["matched_groups"] = _result_value(result, "matched_groups")
        state["allowed_columns"] = await load_columns_for_tables(
            tables=state["tables_in_scope"],
            settings=settings,
        )
        observation = (
            f"Retrieved {len(state['tables_in_scope'])} tables: "
            f"{', '.join(state['tables_in_scope'])}. "
            "tables_in_scope updated. Context updated. Columns refreshed."
        )
        return observation, []

    if action == ReActAction.FETCH_SCHEMA:
        table_names = [table.strip() for table in action_input.split(",")]
        new_cols = await load_columns_for_tables(
            tables=table_names,
            settings=settings,
        )
        state["allowed_columns"].update(new_cols)
        if new_cols:
            lines = [
                f"{table}: {', '.join(columns)}"
                for table, columns in new_cols.items()
            ]
            observation = "Fetched columns:\n" + "\n".join(lines)
        else:
            observation = "MySQL unreachable. No columns fetched."
        return observation, []

    if action == ReActAction.GENERATE_SQL:
        generation_count = state["sql_generation_count"]
        if state["current_sql"] and state["last_validation_errors"]:
            prompt = build_refinement_prompt(
                query=query,
                context=state["context"],
                tables_in_scope=state["tables_in_scope"],
                dialect=settings.sql_dialect,
                previous_sql=state["current_sql"],
                validation_errors=state["last_validation_errors"],
                attempt=generation_count,
                planner_instruction=action_input,
                settings=settings,
            )
        else:
            prompt = build_sql_prompt(
                query=query,
                context=state["context"],
                tables_in_scope=state["tables_in_scope"],
                allowed_columns=state["allowed_columns"],
                dialect=settings.sql_dialect,
                planner_instruction=action_input,
                settings=settings,
            )
        raw, warnings = await call_ollama(
            prompt=prompt,
            settings=settings,
        )
        if warnings:
            return "SQL generation failed", warnings

        sql = narrow_select_star(
            extract_sql(raw or ""),
            state["allowed_columns"],
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
            tables_used, table_warnings = validate_tables_used(
                sql,
                state["tables_in_scope"],
            )
            state["tables_used"] = tables_used
            warnings.extend(table_warnings)
            warnings.extend(validate_columns_used(sql, state["allowed_columns"]))

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

    if action == ReActAction.ASK_CLARIFICATION:
        return "Agent requested clarification.", []

    return f"Agent gave up: {action_input}", []


async def build_clarification(
    query: str,
    failure_reason: str,
    all_warnings: list[SqlWarning],
    react_trace: ReactTrace,
    settings: Settings,
    stage_latencies_ms: dict[str, int] | None = None,
) -> GenerateSqlClarification:
    del all_warnings
    prompt = f"""
A user asked a database question but SQL generation failed.

User question: "{query}"
Failure reason: "{failure_reason}"

Your job: ask ONE clarifying question and provide
2-3 refined query suggestions that would work better.

Be specific. Use database terms where helpful.
Example: instead of "employee named aman", suggest
"find employee by contact name aman" or
"search member with name containing aman".

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
    fallback_suggestions = [
        "Show recent records from the most relevant table",
        "Search by a specific column value",
    ]

    question = fallback_question
    suggestions = fallback_suggestions
    try:
        client = get_model_client(
            settings=settings,
            model=settings.reasoning_model,
            default_timeout=settings.reasoning_timeout,
            role="reasoning",
        )
        response = await client.generate(
            prompt=prompt,
            max_tokens=300,
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

    retrieval_started = time.monotonic()
    await _emit_trace(
        trace_callback,
        stage="schema_retrieval",
        status="started",
        message="Retrieving schema groups and live columns.",
        details={"top_k": top_k or settings.top_k},
    )
    result = await retrieve_groups(
        query=query,
        top_k=top_k or settings.top_k,
        pool=pool,
        search_query=search_query,
    )

    tables_in_scope = _result_value(result, "tables_in_scope")
    initial_columns = await load_columns_for_tables(
        tables=tables_in_scope,
        settings=settings,
    )
    stage_latencies_ms["initial_retrieval"] = int((time.monotonic() - retrieval_started) * 1000)
    await _emit_trace(
        trace_callback,
        stage="schema_retrieval",
        status="completed",
        message=f"Retrieved {len(tables_in_scope)} table(s) for SQL planning.",
        duration_ms=stage_latencies_ms["initial_retrieval"],
        details={
            "tables_in_scope": tables_in_scope,
            "matched_groups": _result_value(result, "matched_groups"),
            "column_tables": sorted(initial_columns.keys()),
        },
    )

    state: dict[str, Any] = {
        "context": _result_value(result, "context"),
        "tables_in_scope": tables_in_scope,
        "matched_groups": _result_value(result, "matched_groups"),
        "allowed_columns": initial_columns,
        "current_sql": None,
        "last_validation_errors": [],
        "sql_generation_count": 0,
        "tables_used": [],
        "top_k": top_k or settings.top_k,
        "search_query": search_query,
    }

    steps: list[ReActStep] = []
    all_warnings: list[SqlWarning] = []
    current_error = ""
    last_completed_action = ReActAction.GIVE_UP

    max_iterations = settings.react_max_iterations
    for iteration in range(1, max_iterations + 1):
        prompt = build_react_prompt(
            query=query,
            context=state["context"],
            tables_in_scope=state["tables_in_scope"],
            allowed_columns=state["allowed_columns"],
            history=steps,
            current_error=current_error,
            dialect=settings.sql_dialect,
            settings=settings,
        )

        step_started = time.monotonic()
        await _emit_trace(
            trace_callback,
            stage="react_iteration",
            status="started",
            message=f"Planning iteration {iteration} started.",
            details={"iteration": iteration},
        )
        thought, answer, reason_warnings = await call_reasoning_model(prompt, settings)
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
        if action == ReActAction.VALIDATE_AND_RETURN and state["last_validation_errors"]:
            action = ReActAction.GENERATE_SQL
            action_input = (
                "Previous SQL failed validation; regenerate SQL to fix validation errors."
            )
        observation, action_warnings = await execute_action(
            action=action,
            action_input=action_input,
            query=query,
            pool=pool,
            settings=settings,
            state=state,
        )
        completed_action = action

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
                attempt_count=iteration,
                react_trace=trace,
                stage_latencies_ms=stage_latencies_ms,
            )

        if action in {ReActAction.GIVE_UP, ReActAction.ASK_CLARIFICATION}:
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
                status="needs_context" if action == ReActAction.ASK_CLARIFICATION else "failed",
                message=action_input or "Could not generate valid SQL.",
                details={
                    "action": action.value,
                    "tables_in_scope": state.get("tables_in_scope", []),
                    "matched_groups": state.get("matched_groups", []),
                },
            )
            return await build_clarification(
                query=query,
                failure_reason=action_input or "Could not generate valid SQL",
                all_warnings=all_warnings,
                react_trace=trace,
                settings=settings,
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
                },
            )
            return GenerateSqlSuccess(
                sql=state["current_sql"],
                warnings=info_warnings,
                tables_used=state["tables_used"],
                matched_groups=state["matched_groups"],
                attempt_count=iteration,
                react_trace=trace,
                stage_latencies_ms=stage_latencies_ms,
            )

    if len(steps) >= max_iterations and (
        not steps
        or steps[-1].action not in {ReActAction.GIVE_UP, ReActAction.ASK_CLARIFICATION}
    ):
        all_warnings.append(
            SqlWarning(
                code=WarningCode.MAX_RETRIES_EXCEEDED,
                message=(
                    "ReAct loop exhausted after "
                    f"{len(steps)} iterations without valid SQL."
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
        },
    )
    return await build_clarification(
        query=query,
        failure_reason=failure_reason,
        all_warnings=all_warnings,
        react_trace=trace,
        settings=settings,
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
