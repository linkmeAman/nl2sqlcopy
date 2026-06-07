from __future__ import annotations

import json
import logging
import re

import asyncpg

from nl2sql_service import instruction_store
from nl2sql_service.config import Settings
from nl2sql_service.llm import get_model_client
from nl2sql_service.roles import LLMRole
from nl2sql_service.synonym_map import expand_query_with_synonyms

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_SQL_START_RE = re.compile(
    r"^\s*(SELECT|WITH|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b",
    re.IGNORECASE,
)
_DEFAULT_MAX_REWRITE_CHARS = 400


def parse_static_hints(raw: str) -> list[str]:
    """Turn env-configured hints into prompt-friendly term mappings."""
    hints: list[str] = []
    for item in re.split(r"[;\n]+", raw or ""):
        item = item.strip()
        if not item:
            continue
        if "->" not in item:
            hints.append(item)
            continue

        left, right = item.split("->", 1)
        right = right.strip()
        if not right:
            hints.append(item)
            continue
        terms = [term.strip() for term in left.split(",") if term.strip()]
        hints.extend(f"{term} -> {right}" for term in terms)
    return hints


async def build_rewrite_hints(
    pool: asyncpg.Pool,
    settings: Settings,
) -> list[str]:
    hints = parse_static_hints(settings.query_rewrite_hints)
    dynamic_hints = await instruction_store.get_rewrite_term_mapping_hints(
        pool=pool,
        min_confidence=settings.min_instruction_confidence,
    )
    for hint in dynamic_hints:
        if hint not in hints:
            hints.append(hint)
    return hints


def build_rewrite_prompt(query: str, hints: list[str]) -> str:
    hint_text = "\n".join(f"- {hint}" for hint in hints[:20]) or "- none"
    return f"""
Rewrite the user question for vector embedding search in an NL2SQL system.

Rules:
- Preserve all names, numbers, dates, statuses, quoted text, and other literals.
- Do not write SQL.
- Do not answer the question.
- Expand business words into likely schema/table terms using the hints.
- Keep the original wording and append useful schema terms when helpful.
- If no expansion is useful, return the original question.

Business/schema term hints:
{hint_text}

User question:
{query}

Return JSON only with this shape:
{{"search_query": "<rewritten search text>"}}
""".strip()


def _extract_json_payload(raw: str) -> str:
    cleaned = _THINK_BLOCK_RE.sub("", raw or "").strip()
    fence_match = _JSON_FENCE_RE.search(cleaned)
    if fence_match:
        return fence_match.group(1).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and start < end:
        return cleaned[start : end + 1]
    return cleaned


def _candidate_from_payload(payload: object) -> str | None:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return None

    for key in ("search_query", "rewritten_query", "query", "expanded_query"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def parse_rewrite_response(
    raw: str,
    original_query: str,
    max_chars: int = _DEFAULT_MAX_REWRITE_CHARS,
) -> str | None:
    """Parse the Ollama rewrite response; return None when fallback is safer."""
    try:
        payload = json.loads(_extract_json_payload(raw))
    except (TypeError, ValueError):
        return None

    candidate = _candidate_from_payload(payload)
    if candidate is None:
        return None

    rewritten = " ".join(candidate.split())
    if not rewritten:
        return None
    if len(rewritten) > max_chars:
        return None
    if _SQL_START_RE.match(rewritten):
        return None

    return rewritten


async def _call_rewrite_model(
    query: str,
    hints: list[str],
    settings: Settings,
) -> str | None:
    client = get_model_client(
        settings=settings,
        model=settings.effective_query_rewrite_model,
        default_timeout=settings.query_rewrite_timeout,
        role=LLMRole.QUERY_REWRITE.value,
    )
    response = await client.generate(
        prompt=build_rewrite_prompt(query, hints),
        max_tokens=settings.query_rewrite_max_tokens,
        temperature=0.0,
        enable_thinking=False,
        timeout=settings.query_rewrite_timeout,
        response_format="json",
    )
    if not response.text:
        logger.info(
            "Query rewrite returned no text; using original query: %s",
            response.error_message or "unknown reason",
        )
        return None

    max_chars = max(_DEFAULT_MAX_REWRITE_CHARS, min(800, len(query) * 4))
    return parse_rewrite_response(
        response.text,
        original_query=query,
        max_chars=max_chars,
    )


async def rewrite_search_query(
    query: str,
    pool: asyncpg.Pool,
    settings: Settings,
) -> str:
    """Return the embedding search text, falling back to *query* on any failure."""
    original = " ".join(query.split())
    if not original:
        return query
    if not settings.query_rewrite_enabled:
        return query
    if len(original.split()) <= 2:
        logger.info("Skipping rewrite for short query: '%s'", original)
        return expand_query_with_synonyms(query, settings)

    try:
        hints = await build_rewrite_hints(pool, settings)
        rewritten = await _call_rewrite_model(original, hints, settings)
    except Exception as exc:  # noqa: BLE001
        logger.info("Query rewrite failed; using original query: %s", exc)
        return expand_query_with_synonyms(query, settings)

    if not rewritten:
        return expand_query_with_synonyms(query, settings)
    return expand_query_with_synonyms(rewritten, settings)
