from __future__ import annotations

import re
from typing import Any

from nl2sql_service.config import Settings, settings as default_settings
from nl2sql_service.model_client import get_model_client
from nl2sql_service.models import SqlWarning, WarningCode
from nl2sql_service.rulebook import build_governance_block, get_config
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


def _format_template_rows(
    columns: list[str],
    rows: list[tuple[Any, ...]],
    max_rows: int = 10,
) -> str:
    if not rows:
        return "(no rows)"

    rendered_rows: list[str] = []
    for row_index, row in enumerate(rows[:max_rows], start=1):
        values = []
        for col_index, column in enumerate(columns):
            value = None if col_index >= len(row) else row[col_index]
            rendered = "NULL" if value is None else str(value)
            values.append(f"{column}={rendered}")
        rendered_rows.append(f"Row {row_index}: {', '.join(values)}")
    return "\n".join(rendered_rows)


def _rows_to_dicts(columns: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            column: None if index >= len(row) else row[index]
            for index, column in enumerate(columns)
        }
        for row in rows
    ]


def _parse_answer_template(text: str) -> dict[str, str] | None:
    sections: dict[str, str] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(
            r"^(ANSWER|KEY\s+FIGURES|DETAILS)\s*:\s*(.*)$",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            current = re.sub(r"\s+", " ", match.group(1).upper())
            sections[current] = match.group(2).strip()
            continue
        if current:
            sections[current] = f"{sections[current]} {line}".strip()

    required = {"ANSWER", "KEY FIGURES", "DETAILS"}
    if not required.issubset(sections):
        return None
    return sections


def _combine_answer_sections(sections: dict[str, str]) -> str:
    parts: list[str] = []
    answer = sections.get("ANSWER", "").strip()
    key_figures = sections.get("KEY FIGURES", "").strip()
    details = sections.get("DETAILS", "").strip()

    if answer and answer.lower() != "none":
        parts.append(answer.rstrip("."))
    if key_figures and key_figures.lower() != "none":
        parts.append(f"Key figures: {key_figures.rstrip('.')}")
    if details and details.lower() != "none":
        parts.append(f"Details: {details.rstrip('.')}")

    if not parts:
        return ""
    return ". ".join(parts).strip() + "."


def _fallback_column_indexes(query: str, columns: list[str], max_columns: int = 8) -> list[int]:
    return select_relevant_column_indexes(query, columns, max_columns)


def _truncate_to_max_words(text: str, max_words: int) -> str:
    if max_words <= 0:
        return text.strip()
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).strip()


def _enforce_answer_style(text: str, settings: Settings) -> str:
    cleaned = text.strip()
    if settings.answer_strict_concise:
        prefixes = (
            "okay, let's tackle this.",
            "let's tackle this.",
            "here's the answer:",
            "based on the result",
        )
        lowered = cleaned.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                cleaned = cleaned[len(prefix):].lstrip(" :\n")
                lowered = cleaned.lower()

    return _truncate_to_max_words(cleaned, settings.answer_max_words)


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
    settings: Settings,
) -> str:
    del sql, warnings
    active_settings = settings or default_settings
    rows_formatted = _format_template_rows(columns, rows, max_rows=10)
    prompt = f"""
You are a concise data analyst. Answer using ONLY the
data provided. Do not add information not in the data.

QUESTION: {query}

DATA ({row_count} rows):
Columns: {", ".join(columns) if columns else "(none)"}
{rows_formatted}

Fill this template exactly. Do not write anything else:

ANSWER: <one sentence directly answering the question>
KEY FIGURES: <any counts, totals, amounts from the data,
              or "none" if not applicable>
DETAILS: <up to 2 specific facts from the data,
          or "none" if not useful>
""".strip()
    if active_settings.governance_enabled and active_settings.governance_inject_answer:
        governance = build_governance_block(
            get_config(active_settings),
            context="answer",
        )
        if governance:
            return f"{prompt}\n\n{governance}"
    return prompt


def validate_answer_numbers(
    answer: str,
    rows: list[dict],
) -> list[str]:
    numbers = re.findall(r"\b\d+(?:\.\d+)?\b", answer)
    violations: list[str] = []
    cell_values = [str(value) for row in rows for value in row.values()]
    for number in numbers:
        if not any(number in value for value in cell_values):
            violations.append(f"Number '{number}' in answer not found in data")
    return violations


async def call_answer_model(
    prompt: str,
    settings: Settings,
) -> tuple[str | None, list[SqlWarning]]:
    model_name = settings.answer_model or settings.reasoning_model
    client = get_model_client(
        settings=settings,
        model=model_name,
        default_timeout=settings.answer_timeout,
    )
    response = await client.generate(
        prompt=prompt,
        max_tokens=settings.answer_max_tokens,
        temperature=settings.answer_temperature,
        enable_thinking=settings.answer_allow_reasoning,
        timeout=settings.answer_timeout,
    )
    if not response.text:
        code = (
            WarningCode.ANSWER_TIMEOUT
            if response.error_type == "timeout"
            else WarningCode.ANSWER_MALFORMED
            if response.error_type in {"malformed", "empty"}
            else WarningCode.ANSWER_UPSTREAM
        )
        detail = response.error_message or f"Answer model returned no text from {client.provider_name}"
        return None, [
            SqlWarning(
                code=code,
                message=detail,
            )
        ]

    return response.text.strip(), []


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
        settings=settings,
    )
    answer_text, warnings = await call_answer_model(prompt=prompt, settings=settings)
    if answer_text is not None:
        sections = _parse_answer_template(answer_text)
        if sections is not None:
            rendered = _combine_answer_sections(sections)
            if rendered:
                answer = _enforce_answer_style(rendered, settings)
                violations = validate_answer_numbers(answer, _rows_to_dicts(columns, rows))
                if violations:
                    warnings = [
                        *warnings,
                        SqlWarning(
                            code=WarningCode.ANSWER_HALLUCINATION,
                            message=(
                                "Answer may contain invented numbers: "
                                f"{violations}"
                            ),
                        ),
                    ]
                return answer, warnings

    return build_fallback_answer(query, columns, rows, row_count), warnings
