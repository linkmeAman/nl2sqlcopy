from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from nl2sql_service.core.config import Settings

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _resolve_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()


@lru_cache(maxsize=16)
def _load_raw(path_value: str) -> dict:
    path = _resolve_path(path_value)
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _normalize_key(value: str) -> str:
    return _NON_ALNUM_RE.sub("", value.strip().lower())


def _normalize_phrase(value: str) -> str:
    return " ".join(
        part
        for part in _NON_ALNUM_RE.sub(" ", value.strip().lower()).split()
        if part
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = " ".join(str(value).split()).strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def load_synonym_sections(settings: Settings) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    payload = _load_raw(settings.query_rewrite_synonym_map)
    query_terms = payload.get("query_terms", {})
    column_terms = payload.get("column_terms", {})

    normalized_query_terms = {
        _normalize_key(str(key)): _dedupe([str(item) for item in values])
        for key, values in query_terms.items()
        if isinstance(values, list) and _normalize_key(str(key))
    }
    normalized_column_terms = {
        _normalize_key(str(key)): _dedupe([str(item) for item in values])
        for key, values in column_terms.items()
        if isinstance(values, list) and _normalize_key(str(key))
    }
    return normalized_query_terms, normalized_column_terms


def split_identifier_parts(identifier: str) -> list[str]:
    expanded = _CAMEL_BOUNDARY_RE.sub(" ", identifier or "")
    return [
        part
        for part in _NON_ALNUM_RE.sub(" ", expanded.lower()).split()
        if part
    ]


def aliases_for_column_introspection(column_name: str) -> list[str]:
    """
    Produces aliases using only the column name itself.
    No external vocabulary. No synonyms.json access.
    Safe for use in schema enrichment and column catalog ingestion.
    """
    parts = split_identifier_parts(column_name)
    aliases: list[str] = []
    humanized = " ".join(parts)
    if humanized and humanized != column_name:
        aliases.append(humanized)
    for part in parts:
        if len(part) > 2 and part != column_name:
            aliases.append(part)
    return list(dict.fromkeys(aliases))


def aliases_for_column_rewrite(column_name: str, settings: Settings) -> list[str]:
    """
    Produces aliases using column name AND external synonym vocabulary.
    Only safe for query rewrite expansion, never for DB introspection.
    """
    _, column_terms = load_synonym_sections(settings)
    normalized_key = _normalize_key(column_name)
    parts = split_identifier_parts(column_name)

    aliases: list[str] = []
    humanized = " ".join(parts)
    if humanized and humanized != normalized_key:
        aliases.append(humanized)

    for key in {normalized_key, *(_normalize_key(part) for part in parts)}:
        aliases.extend(column_terms.get(key, []))

    return _dedupe(aliases)


def aliases_for_column(column_name: str, settings: Settings) -> list[str]:
    """Compatibility shim for existing rewrite-oriented call sites."""
    return aliases_for_column_rewrite(column_name, settings)


def expand_query_with_synonyms(query: str, settings: Settings) -> str:
    query_terms, _ = load_synonym_sections(settings)
    normalized_query = _normalize_phrase(query)
    expansions: list[str] = []

    for raw_key, synonyms in query_terms.items():
        key_phrase = _normalize_phrase(raw_key)
        if not key_phrase:
            continue
        if not re.search(rf"(?<!\w){re.escape(key_phrase)}(?!\w)", normalized_query):
            continue
        expansions.extend(synonyms)

    unique_expansions = [
        term
        for term in _dedupe(expansions)
        if _normalize_phrase(term) not in normalized_query
    ]
    if not unique_expansions:
        return query
    return f"{query} {' '.join(unique_expansions)}".strip()
