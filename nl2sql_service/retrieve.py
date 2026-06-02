from __future__ import annotations

import json
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
        rows = await conn.fetch(_QUERY_SQL, query_vec, top_k)

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
        rows = await conn.fetch(_GROUP_QUERY_SQL, query_vec, top_k)

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
