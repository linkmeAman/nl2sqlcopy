from __future__ import annotations

import re
from typing import Any

import asyncpg
import httpx

from nl2sql_service.column_loader import load_columns_for_tables
from nl2sql_service.config import Settings
from nl2sql_service.models import (
    GenerateSqlRejected,
    GenerateSqlResponse,
    GenerateSqlSuccess,
    ReActAction,
    ReActStep,
    ReactTrace,
    SqlWarning,
    WarningCode,
)
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


async def call_reasoning_model(
    prompt: str,
    settings: Settings,
) -> tuple[str, str, list[SqlWarning]]:
    url = f"{settings.llm_base_url.rstrip('/')}/api/generate"
    payload = {
        "model": settings.reasoning_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "think": True,
        "options": {
            "temperature": settings.reasoning_temperature,
            "num_predict": 800,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=settings.reasoning_timeout) as client:
            response = await client.post(url, json=payload)
    except httpx.TimeoutException:
        return "", "", [
            SqlWarning(
                code=WarningCode.OLLAMA_TIMEOUT,
                message=(
                    "Reasoning model timed out after "
                    f"{settings.reasoning_timeout}s"
                ),
            )
        ]
    except httpx.RequestError as exc:
        return "", "", [
            SqlWarning(
                code=WarningCode.OLLAMA_UPSTREAM,
                message=f"Reasoning model unreachable: {exc}",
            )
        ]
    except Exception as exc:  # noqa: BLE001
        return "", "", [
            SqlWarning(
                code=WarningCode.OLLAMA_UPSTREAM,
                message=f"Reasoning model unreachable: {exc}",
            )
        ]

    if not response.is_success:
        return "", "", [
            SqlWarning(
                code=WarningCode.OLLAMA_UPSTREAM,
                message=(
                    f"Reasoning model unreachable: HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                ),
            )
        ]

    try:
        data = response.json()
    except ValueError:
        return "", "", [
            SqlWarning(
                code=WarningCode.OLLAMA_MALFORMED,
                message="Reasoning model response missing 'response' field",
            )
        ]

    if not isinstance(data, dict) or not isinstance(data.get("response"), str):
        return "", "", [
            SqlWarning(
                code=WarningCode.OLLAMA_MALFORMED,
                message="Reasoning model response missing 'response' field",
            )
        ]

    thought, answer = extract_think_block(data["response"])
    if isinstance(data.get("thinking"), str) and data["thinking"].strip():
        thinking = data["thinking"].strip()
        if not answer and looks_like_action_payload(thinking):
            answer = thinking
            thought = ""
        else:
            thought = thinking
    return thought, answer, []


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
) -> str:
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

    return f"""
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
- GIVE_UP: cannot generate valid SQL, explain why

USER QUESTION: {query}

RETRIEVED SCHEMA CONTEXT:
{context}

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
- If error is unrecoverable, use GIVE_UP

Think carefully about what went wrong and what to do.
Then output EXACTLY:
{{"action":"<one of the five actions above>","input":"<brief instruction for the action>"}}
""".strip()


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
        result = await retrieve_groups(
            query=refined_query,
            top_k=state["top_k"],
            pool=pool,
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
            )
        else:
            prompt = build_sql_prompt(
                query=query,
                context=state["context"],
                tables_in_scope=state["tables_in_scope"],
                allowed_columns=state["allowed_columns"],
                dialect=settings.sql_dialect,
                planner_instruction=action_input,
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

    observation = f"Agent gave up: {action_input}"
    return observation, [
        SqlWarning(
            code=WarningCode.MAX_RETRIES_EXCEEDED,
            message=f"ReAct agent chose GIVE_UP: {action_input}",
        )
    ]


async def run(
    query: str,
    pool: asyncpg.Pool,
    settings: Settings,
    top_k: int | None = None,
) -> GenerateSqlResponse:
    result = await retrieve_groups(
        query=query,
        top_k=top_k or settings.top_k,
        pool=pool,
    )
    tables_in_scope = _result_value(result, "tables_in_scope")
    initial_columns = await load_columns_for_tables(
        tables=tables_in_scope,
        settings=settings,
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
        )

        thought, answer, reason_warnings = await call_reasoning_model(prompt, settings)
        if reason_warnings:
            all_warnings.extend(reason_warnings)
            break

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

        steps.append(
            ReActStep(
                iteration=iteration,
                thought=thought,
                action=action,
                action_input=action_input,
                observation=observation,
            )
        )
        last_completed_action = completed_action

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
            return GenerateSqlSuccess(
                sql=state["current_sql"],
                warnings=info_warnings,
                tables_used=state["tables_used"],
                matched_groups=state["matched_groups"],
                attempt_count=iteration,
                react_trace=trace,
            )

        if action == ReActAction.GIVE_UP:
            break

    if len(steps) >= max_iterations and (
        not steps or steps[-1].action != ReActAction.GIVE_UP
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
    return GenerateSqlRejected(
        warnings=all_warnings,
        attempt_count=len(steps),
        react_trace=trace,
    )


def _blocking_warnings(warnings: list[SqlWarning]) -> list[SqlWarning]:
    return [
        warning
        for warning in warnings
        if warning.code != WarningCode.MYSQL_EXPLAIN_UNAVAILABLE
    ]


def _result_value(result: Any, field: str) -> Any:
    if isinstance(result, dict):
        return result[field]
    return getattr(result, field)
