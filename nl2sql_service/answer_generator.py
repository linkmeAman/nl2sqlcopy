from __future__ import annotations

from typing import Any

import httpx

from nl2sql_service.config import Settings
from nl2sql_service.models import SqlWarning, WarningCode
from nl2sql_service.react_agent import extract_think_block
from nl2sql_service.sql_generator import select_relevant_column_indexes


def _format_warning_lines(warnings: list[SqlWarning]) -> str:
    if not warnings:
        return "(none)"
    return "\n".join(f"- {warning.code.value}: {warning.message}" for warning in warnings)


def _format_result_table(
    columns: list[str],
    rows: list[tuple[Any, ...]],
    column_indexes: list[int] | None = None,
    max_rows: int = 10,
) -> str:
    if not columns:
        return "(no columns)"

    indexes = (
        column_indexes
        if column_indexes is not None
        else list(range(min(8, len(columns))))
    )
    display_columns = [columns[index] for index in indexes]
    header = " | ".join(display_columns)
    if len(columns) > len(display_columns):
        header += f" | ... ({len(columns) - len(display_columns)} hidden columns)"
    rendered_rows: list[str] = [header]
    for row in rows[:max_rows]:
        rendered = [
            "NULL" if index >= len(row) or row[index] is None else str(row[index])
            for index in indexes
        ]
        if len(columns) > len(display_columns):
            rendered.append("...")
        rendered_rows.append(" | ".join(rendered))
    if len(rows) > max_rows:
        rendered_rows.append(f"... {len(rows) - max_rows} more rows")
    return "\n".join(rendered_rows)


def _fallback_column_indexes(query: str, columns: list[str], max_columns: int = 8) -> list[int]:
    return select_relevant_column_indexes(query, columns, max_columns)


def build_fallback_answer(
    query: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    row_count: int,
) -> str:
    if row_count == 0:
        return "No rows matched the question."
    if not columns:
        return f"Found {row_count} row{'s' if row_count != 1 else ''}."

    indexes = _fallback_column_indexes(query, columns)
    display_columns = [columns[index] for index in indexes]
    lines = [
        f"Found {row_count} row{'s' if row_count != 1 else ''}.",
        "",
        " | ".join(display_columns),
    ]
    for row in rows[:10]:
        lines.append(
            " | ".join(
                "NULL" if index >= len(row) or row[index] is None else str(row[index])
                for index in indexes
            )
        )
    if row_count > 10:
        lines.append(f"... {row_count - 10} more rows")
    return "\n".join(lines)


def build_answer_prompt(
    query: str,
    sql: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    row_count: int,
    warnings: list[SqlWarning],
) -> str:
    display_indexes = _fallback_column_indexes(query, columns)
    display_columns = [columns[index] for index in display_indexes]
    return "\n".join(
        [
            "You are an assistant that summarizes SQL query results for business users.",
            "Answer in at most 80 words.",
            "For show/list queries, use compact numbered lines.",
            "Do not output SQL unless the user explicitly asked for SQL.",
            "If no rows are returned, say that clearly.",
            "Use only the displayed result rows and columns.",
            "",
            "User question:",
            query,
            "",
            "Executed SQL:",
            sql,
            "",
            f"Row count: {row_count}",
            "Displayed columns:",
            ", ".join(display_columns) if display_columns else "(none)",
            "",
            "Result rows (first 10 rows, selected columns):",
            "```text",
            _format_result_table(columns, rows, display_indexes, max_rows=10),
            "```",
            "",
            "Warnings:",
            _format_warning_lines(warnings),
            "",
            "Return only the final answer text.",
        ]
    )


async def call_answer_model(
    prompt: str,
    settings: Settings,
) -> tuple[str | None, list[SqlWarning]]:
    url = f"{settings.llm_base_url.rstrip('/')}/api/generate"
    model_name = settings.answer_model or settings.reasoning_model
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": settings.answer_temperature,
            "num_predict": 300,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=settings.answer_timeout) as client:
            response = await client.post(url, json=payload)
    except httpx.TimeoutException:
        return None, [
            SqlWarning(
                code=WarningCode.ANSWER_TIMEOUT,
                message=f"Answer model timed out after {settings.answer_timeout}s",
            )
        ]
    except httpx.RequestError as exc:
        return None, [
            SqlWarning(
                code=WarningCode.ANSWER_UPSTREAM,
                message=f"Answer model unreachable: {exc}",
            )
        ]

    if not response.is_success:
        return None, [
            SqlWarning(
                code=WarningCode.ANSWER_UPSTREAM,
                message=(
                    f"Answer model unreachable: HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                ),
            )
        ]

    try:
        data = response.json()
    except ValueError:
        return None, [
            SqlWarning(
                code=WarningCode.ANSWER_MALFORMED,
                message="Answer model response missing 'response' field",
            )
        ]

    raw_response = data.get("response") if isinstance(data, dict) else None
    if not isinstance(raw_response, str):
        return None, [
            SqlWarning(
                code=WarningCode.ANSWER_MALFORMED,
                message="Answer model response missing 'response' field",
            )
        ]

    _, answer = extract_think_block(raw_response)
    answer_text = answer.strip()
    if not answer_text:
        return None, [
            SqlWarning(
                code=WarningCode.ANSWER_MALFORMED,
                message="Answer model returned empty answer",
            )
        ]

    return answer_text, []


async def generate_answer(
    query: str,
    sql: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    row_count: int,
    sql_warnings: list[SqlWarning],
    settings: Settings,
) -> tuple[str | None, list[SqlWarning]]:
    prompt = build_answer_prompt(
        query=query,
        sql=sql,
        columns=columns,
        rows=rows,
        row_count=row_count,
        warnings=sql_warnings,
    )
    answer_text, warnings = await call_answer_model(prompt=prompt, settings=settings)
    if answer_text is not None:
        return answer_text, warnings

    return build_fallback_answer(query, columns, rows, row_count), warnings
