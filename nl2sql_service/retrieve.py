from __future__ import annotations

import json
import re
import time
import asyncpg

from nl2sql_service import embed
from nl2sql_service.cache import embed_cache
from nl2sql_service import instruction_store
from nl2sql_service import pattern_store
from nl2sql_service.config import settings
from nl2sql_service.models import GroupQueryResponse, QueryResult
from nl2sql_service.observability.context import emit_current_trace_event
from nl2sql_service.observability.metrics import observe_retrieval
from nl2sql_service.observability.sanitization import summarize_text

_SCHEMA_QUERY_STOPWORDS = {
    "a",
    "all",
    "an",
    "and",
    "by",
    "data",
    "details",
    "entries",
    "entry",
    "fetch",
    "find",
    "for",
    "from",
    "get",
    "give",
    "latest",
    "list",
    "me",
    "most",
    "new",
    "newest",
    "of",
    "please",
    "recent",
    "records",
    "report",
    "results",
    "rows",
    "show",
    "the",
    "with",
}

_QUERY_SQL = """
SELECT
    content,
    1 - (embedding <=> $1) AS similarity,
    source,
    chunk_index,
    token_count,
    embedding_model,
    metadata
FROM nl2sql_embeddings
ORDER BY embedding <=> $1
LIMIT $2
"""


async def _fetch_vector_rows(
    conn: asyncpg.Connection,
    sql: str,
    query_vec: list[float],
    top_k: int,
    *extra_params: object,
) -> list[asyncpg.Record]:
    ef_search = max(1, int(settings.vector_hnsw_ef_search))
    transaction = getattr(conn, "transaction", None)
    if transaction is None:
        return await conn.fetch(sql, query_vec, top_k, *extra_params)
    async with transaction():
        await conn.execute(f"SET LOCAL hnsw.ef_search = {ef_search}")
        return await conn.fetch(sql, query_vec, top_k, *extra_params)


async def _embed_for_retrieval(text: str) -> list[float]:
    await emit_current_trace_event(
        event="embedding_generation_started",
        stage="retrieval_embedding",
        status="started",
        message="Generating embedding for retrieval.",
        input_summary={"search_query_preview": summarize_text(text)},
        metadata={"embedding_provider": settings.embedding_provider, "embedding_model": settings.embedding_model},
    )
    started = time.monotonic()
    if settings.embed_cache_enabled:
        cached = embed_cache.get(text)
        if cached:
            await emit_current_trace_event(
                event="embedding_generation_completed",
                stage="retrieval_embedding",
                status="completed",
                message="Reused cached retrieval embedding.",
                duration_ms=int((time.monotonic() - started) * 1000),
                metadata={"cache_hit": True},
            )
            return cached

    vectors = await embed.embed_texts([text])
    vector = vectors[0]
    if settings.embed_cache_enabled:
        embed_cache.set(text, vector)
    await emit_current_trace_event(
        event="embedding_generation_completed",
        stage="retrieval_embedding",
        status="completed",
        message="Generated retrieval embedding.",
        duration_ms=int((time.monotonic() - started) * 1000),
        metadata={"cache_hit": False, "dimension": len(vector)},
    )
    return vector


async def retrieve(
    query: str,
    top_k: int,
    pool: asyncpg.Pool,
    search_query: str | None = None,
) -> list[QueryResult]:
    """
    Embed *search_query* or *query* and return similar chunks from pgvector.

    Similarity is cosine similarity expressed as ``1 - cosine_distance``,
    so 1.0 is an exact match and −1.0 is the opposite direction.
    """
    retrieval_started = time.monotonic()
    query_vec = await _embed_for_retrieval(search_query or query)
    await emit_current_trace_event(
        event="vector_search_started",
        stage="vector_search",
        status="started",
        message="Vector search started.",
        metadata={"top_k": top_k, "search_query_preview": summarize_text(search_query or query)},
    )

    async with pool.acquire() as conn:
        rows = await _fetch_vector_rows(conn, _QUERY_SQL, query_vec, top_k)

    results: list[QueryResult] = []
    for row in rows:
        raw = row["metadata"]
        if not raw:
            metadata = {}
        elif isinstance(raw, str):
            metadata = json.loads(raw)
        else:
            metadata = dict(raw)
        metadata["source"] = row["source"]
        metadata["chunk_index"] = row["chunk_index"]
        metadata["token_count"] = row["token_count"]
        metadata["embedding_model"] = row["embedding_model"]
        results.append(
            QueryResult(
                content=row["content"],
                similarity=float(row["similarity"]),
                metadata=metadata,
            )
        )

    observe_retrieval("completed")
    await emit_current_trace_event(
        event="vector_search_completed",
        stage="vector_search",
        status="completed",
        message="Vector search completed.",
        duration_ms=int((time.monotonic() - retrieval_started) * 1000),
        output_summary={
            "retrieved_chunks": len(results),
            "similarity_scores": [round(result.similarity, 4) for result in results],
        },
    )
    return results


# ---------------------------------------------------------------------------
# Schema-group retrieval
# ---------------------------------------------------------------------------

_GROUP_QUERY_SQL = """
SELECT
    content,
    1 - (embedding <=> $1) AS similarity,
    source,
    chunk_index,
    token_count,
    embedding_model,
    metadata
FROM nl2sql_embeddings
WHERE metadata->>'type' = 'schema_group'
ORDER BY embedding <=> $1
LIMIT $2
"""


_COLUMN_QUERY_SQL = """
SELECT
    content,
    1 - (embedding <=> $1) AS similarity,
    source,
    chunk_index,
    token_count,
    embedding_model,
    metadata
FROM nl2sql_embeddings
WHERE metadata->>'type' = 'column_catalog'
  AND COALESCE(metadata->>'table_name', metadata->>'object_name') = ANY($3::text[])
ORDER BY embedding <=> $1
LIMIT $2
"""


async def retrieve_groups(
    query: str,
    top_k: int,
    pool: asyncpg.Pool,
    search_query: str | None = None,
) -> GroupQueryResponse:
    """
    Embed *search_query* or *query* and return closest schema-group chunks.

    Builds three output artefacts:

    * ``matched_groups`` – ordered list of group source names by similarity.
    * ``tables_in_scope`` – deduplicated union of ``tables`` + ``related_tables``
      from all matched groups, preserving insertion order.
    * ``context`` – formatted multi-block string ready to paste into an LLM prompt.
    """
    retrieval_started = time.monotonic()
    query_vec = await _embed_for_retrieval(search_query or query)
    await emit_current_trace_event(
        event="vector_search_started",
        stage="schema_retrieval",
        status="started",
        message="Schema-group vector search started.",
        metadata={"top_k": top_k, "search_query_preview": summarize_text(search_query or query)},
    )

    async with pool.acquire() as conn:
        rows = await _fetch_vector_rows(conn, _GROUP_QUERY_SQL, query_vec, top_k)

    rows = _rerank_schema_group_rows(query=query, rows=rows)

    results: list[QueryResult] = []
    matched_groups: list[str] = []
    seen_tables: dict[str, None] = {}  # ordered-set via insertion-order dict
    context_blocks: list[str] = []

    for row in rows:
        raw = row["metadata"]
        if not raw:
            metadata: dict = {}
        elif isinstance(raw, str):
            metadata = json.loads(raw)
        else:
            metadata = dict(raw)

        metadata["source"] = row["source"]
        metadata["chunk_index"] = row["chunk_index"]
        metadata["token_count"] = row["token_count"]
        metadata["embedding_model"] = row["embedding_model"]

        results.append(
            QueryResult(
                content=row["content"],
                similarity=float(row["similarity"]),
                metadata=metadata,
            )
        )

        group_name: str = row["source"]
        matched_groups.append(group_name)

        # Accumulate tables in insertion order (primary first, then related)
        for tbl in metadata.get("tables", []):
            seen_tables.setdefault(tbl, None)
        for tbl in metadata.get("related_tables", []):
            seen_tables.setdefault(tbl, None)

        # Build a context block for this group
        sim_pct = f"{float(row['similarity']) * 100:.1f}%"
        description = metadata.get("group_description", "")
        tables_line = ", ".join(metadata.get("tables", []))
        related_line = ", ".join(metadata.get("related_tables", []))
        block_lines = [
            f"## Schema group: {group_name} (similarity {sim_pct})",
            f"Description: {description}",
            f"Tables: {tables_line}",
        ]
        if related_line:
            block_lines.append(f"Related tables: {related_line}")
        block_lines.append("")
        block_lines.append(row["content"])
        context_blocks.append("\n".join(block_lines))

    schema_context = "\n\n---\n\n".join(context_blocks)
    context = schema_context
    patterns = await pattern_store.get_relevant_patterns(
        query=query,
        tables_in_scope=list(seen_tables),
        pool=pool,
        min_use_count=settings.min_pattern_use_count,
    )
    if patterns:
        pattern_text = pattern_store.format_patterns_for_prompt(patterns)
        if pattern_text:
            context = "PREVIOUSLY LEARNED PATTERNS:\n" + pattern_text + "\n\n" + context

    instructions = await instruction_store.get_relevant_instructions(
        query=query,
        tables_in_scope=list(seen_tables),
        pool=pool,
        min_confidence=settings.min_instruction_confidence,
    )
    if instructions:
        instruction_text = instruction_store.format_instructions_for_prompt(instructions)
        if instruction_text:
            context = instruction_text + "\n\n" + context

    await emit_current_trace_event(
        event="vector_search_completed",
        stage="schema_retrieval",
        status="completed",
        message="Schema-group vector search completed.",
        duration_ms=int((time.monotonic() - retrieval_started) * 1000),
        output_summary={
            "matched_groups": matched_groups,
            "selected_tables": list(seen_tables),
            "retrieved_chunks": len(results),
            "similarity_scores": [round(result.similarity, 4) for result in results],
        },
        metadata={
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
        },
    )
    observe_retrieval("completed")
    return GroupQueryResponse(
        matched_groups=matched_groups,
        tables_in_scope=list(seen_tables),
        context=context,
        results=results,
    )


def _singularize_token(token: str) -> str:
    if token.endswith("ies") and len(token) > 3:
        return f"{token[:-3]}y"
    if token.endswith("ses") and len(token) > 3:
        return token[:-2]
    if token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
        return token[:-1]
    return token


def _identifier_forms(value: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    if not normalized:
        return set()
    parts = [part for part in normalized.split("_") if part]
    forms = {normalized}
    forms.update(parts)
    forms.update(_singularize_token(part) for part in parts)
    return {form for form in forms if form}


def _query_focus_terms(query: str) -> set[str]:
    raw_terms = [term for term in re.findall(r"[a-z0-9_]+", query.lower()) if term]
    normalized = {
        _singularize_token(term)
        for term in raw_terms
        if term not in _SCHEMA_QUERY_STOPWORDS
    }
    return {term for term in normalized if term}


def _raw_metadata(row: asyncpg.Record) -> dict[str, object]:
    raw = row["metadata"]
    if not raw:
        return {}
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw)


def _schema_group_priority(
    *,
    query_terms: set[str],
    metadata: dict[str, object],
    source: str,
) -> tuple[int, int, int, int]:
    root_forms = _identifier_forms(str(metadata.get("root_table") or ""))
    included_forms: set[str] = set()
    for table in metadata.get("included_tables") or metadata.get("tables") or []:
        included_forms.update(_identifier_forms(str(table)))
    referenced_values = (
        metadata.get("referenced_tables")
        or metadata.get("summarized_tables")
        or metadata.get("related_tables")
        or []
    )
    related_forms: set[str] = set()
    for table in referenced_values:
        related_forms.update(_identifier_forms(str(table)))
    group_forms = _identifier_forms(str(metadata.get("chunk_group_name") or source))

    root_hits = len(query_terms & root_forms)
    included_hits = len(query_terms & included_forms)
    group_hits = len(query_terms & group_forms)
    related_hits = len(query_terms & related_forms)
    related_only_penalty = 1 if related_hits and not (root_hits or included_hits or group_hits) else 0
    return root_hits, included_hits, group_hits, -related_only_penalty


def _rerank_schema_group_rows(
    *,
    query: str,
    rows: list[asyncpg.Record],
) -> list[asyncpg.Record]:
    query_terms = _query_focus_terms(query)
    if not query_terms or len(query_terms) > 4:
        return rows

    scored_rows: list[tuple[tuple[int, int, int, int, float], int, asyncpg.Record]] = []
    has_direct_signal = False
    for index, row in enumerate(rows):
        metadata = _raw_metadata(row)
        priority = _schema_group_priority(
            query_terms=query_terms,
            metadata=metadata,
            source=str(row["source"]),
        )
        if priority[0] or priority[1] or priority[2]:
            has_direct_signal = True
        scored_rows.append(((*priority, float(row["similarity"])), index, row))

    if not has_direct_signal:
        return rows

    scored_rows.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return [row for _, _, row in scored_rows]


async def retrieve_column_catalog(
    *,
    query: str,
    tables: list[str],
    top_k: int,
    pool: asyncpg.Pool,
    search_query: str | None = None,
) -> list[QueryResult]:
    normalized_tables = [
        str(table).strip().lower()
        for table in tables
        if str(table).strip()
    ]
    if not normalized_tables:
        return []

    retrieval_started = time.monotonic()
    query_vec = await _embed_for_retrieval(search_query or query)
    await emit_current_trace_event(
        event="vector_search_started",
        stage="column_retrieval",
        status="started",
        message="Column-catalog vector search started.",
        metadata={
            "top_k": top_k,
            "tables": normalized_tables,
            "search_query_preview": summarize_text(search_query or query),
        },
    )

    async with pool.acquire() as conn:
        rows = await _fetch_vector_rows(
            conn,
            _COLUMN_QUERY_SQL,
            query_vec,
            top_k,
            normalized_tables,
        )

    results: list[QueryResult] = []
    for row in rows:
        raw = row["metadata"]
        if not raw:
            metadata = {}
        elif isinstance(raw, str):
            metadata = json.loads(raw)
        else:
            metadata = dict(raw)
        metadata["source"] = row["source"]
        metadata["chunk_index"] = row["chunk_index"]
        metadata["token_count"] = row["token_count"]
        metadata["embedding_model"] = row["embedding_model"]
        results.append(
            QueryResult(
                content=row["content"],
                similarity=float(row["similarity"]),
                metadata=metadata,
            )
        )

    await emit_current_trace_event(
        event="vector_search_completed",
        stage="column_retrieval",
        status="completed",
        message="Column-catalog vector search completed.",
        duration_ms=int((time.monotonic() - retrieval_started) * 1000),
        output_summary={
            "tables": normalized_tables,
            "retrieved_chunks": len(results),
            "similarity_scores": [round(result.similarity, 4) for result in results],
        },
    )
    return results
