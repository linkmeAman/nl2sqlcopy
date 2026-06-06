from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from nl2sql_service import chunker, db, embed
from nl2sql_service.models import SchemaTable
from nl2sql_service import schema_loader

log = logging.getLogger(__name__)


_GROUP_UPSERT_SQL = """
INSERT INTO nl2sql_embeddings
    (content, embedding, source, chunk_index, token_count, embedding_model, metadata)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (source, chunk_index)
DO UPDATE SET
    content = EXCLUDED.content,
    embedding = EXCLUDED.embedding,
    token_count = EXCLUDED.token_count,
    metadata = EXCLUDED.metadata,
    created_at = NOW()
WHERE nl2sql_embeddings.metadata->>'schema_version'
      != EXCLUDED.metadata->>'schema_version'
RETURNING (xmax = 0) AS inserted, (xmax <> 0) AS updated
"""


_KNOWLEDGE_UPSERT_SQL = _GROUP_UPSERT_SQL


_PENDING_VERSIONED_CHUNKS_SQL = """
WITH incoming AS (
    SELECT source, chunk_index, schema_version
    FROM jsonb_to_recordset($1::jsonb)
        AS item(source text, chunk_index int, schema_version text)
)
SELECT incoming.source, incoming.chunk_index
FROM incoming
LEFT JOIN nl2sql_embeddings existing
    ON existing.source = incoming.source
   AND existing.chunk_index = incoming.chunk_index
WHERE existing.source IS NULL
   OR existing.metadata->>'schema_version' IS DISTINCT FROM incoming.schema_version
"""


async def ensure_hnsw_index(pool: asyncpg.Pool) -> None:
    """
    Ensure the vector index uses HNSW instead of IVFFlat.

    IVFFlat needs a tuned ``lists ~= sqrt(row_count)`` and periodic
    ANALYZE/VACUUM hygiene to maintain recall. HNSW has no list tuning
    parameter and is safer for mixed chunk populations and growing row counts.
    """
    sql = """
    CREATE INDEX IF NOT EXISTS nl2sql_embed_hnsw_idx
      ON nl2sql_embeddings
      USING hnsw (embedding vector_cosine_ops)
      WITH (m=16, ef_construction=64);
    """
    async with pool.acquire() as conn:
        await conn.execute(sql)


async def _filter_chunks_needing_upsert(
    chunks: list[dict],
    pool: asyncpg.Pool,
) -> tuple[list[dict], int]:
    if not chunks:
        return [], 0

    incoming = json.dumps(
        [
            {
                "source": chunk["source"],
                "chunk_index": chunk["chunk_index"],
                "schema_version": chunk.get("schema_version"),
            }
            for chunk in chunks
        ]
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch(_PENDING_VERSIONED_CHUNKS_SQL, incoming)

    pending_keys = {(row["source"], row["chunk_index"]) for row in rows}
    pending_chunks = [
        chunk for chunk in chunks if (chunk["source"], chunk["chunk_index"]) in pending_keys
    ]

    skipped_count = len(chunks) - len(pending_chunks)
    if skipped_count:
        log.info(
            "Skipped %d unchanged versioned chunks before embedding",
            skipped_count,
        )

    return pending_chunks, skipped_count


async def ingest_text(text: str, source: str, pool: asyncpg.Pool) -> int:
    """
    Chunk free text, embed every chunk, and store in pgvector.

    Returns the number of chunks submitted for insertion (including any
    skipped by ON CONFLICT DO NOTHING).
    """
    chunks = chunker.chunk_text(text, source)
    embeddings = await embed.embed_texts([c["text"] for c in chunks])
    inserted = await db.insert_chunks(pool, chunks, embeddings)
    return inserted


async def ingest_schema(
    tables: list[SchemaTable],
    source: str,
    pool: asyncpg.Pool,
) -> int:
    """
    Render one chunk per table, embed, and store in pgvector.

    Returns the number of chunks submitted for insertion.
    """
    chunks = chunker.chunk_schema(tables, source)
    embeddings = await embed.embed_texts([c["text"] for c in chunks])
    inserted = await db.insert_chunks(pool, chunks, embeddings)
    return inserted


async def ingest_schema_groups(
    group_names: list[str] | None,
    pool: asyncpg.Pool,
) -> dict[str, Any]:
    """
    Embed one chunk per semantic group and upsert by ``schema_version``.

    Each group uses ``source=<group_name>`` and ``chunk_index=0``.
    Returns ``(inserted_count, updated_count)``.
    """
    if group_names:
        entities = schema_loader.load_entities()
        resolved_names: list[str] = []
        for name in group_names:
            entity = schema_loader.get_entity(name)
            if entity is None:
                entity = next(
                    (item for item in entities if item.get("chunk_group_name") == name),
                    None,
                )
            if entity is None:
                raise ValueError(f"Entity '{name}' not found in rag_schema/entities/")
            resolved_names.append(str(entity["entity_id"]))

        names = resolved_names
        log.info("Ingesting %d specific groups: %s", len(group_names), group_names)
    else:
        names = schema_loader.get_all_group_names()
        log.info("Ingesting all %d groups from rag_schema/entities/", len(names))

    chunks: list[dict[str, Any]] = []
    failed_groups: list[dict[str, str]] = []
    groups_with_columns = 0
    groups_without_columns = 0
    groups_with_aliases = 0
    groups_with_examples = 0

    for name in names:
        try:
            chunk = await chunker.chunk_schema_group(
                group_name=name,
                settings=chunker.settings,
                allowed_columns=None,
            )
        except ValueError as exc:
            message = str(exc)
            if "Exceeds 400 limit" not in message:
                raise
            failed_groups.append({"group_name": name, "reason": message})
            log.warning("Skipping group '%s' during ingest: %s", name, message)
            continue

        has_columns = bool(chunk.get("has_columns"))
        has_aliases = bool(chunk.get("has_aliases"))
        has_examples = bool(chunk.get("has_examples"))

        if has_columns:
            groups_with_columns += 1
        else:
            groups_without_columns += 1
        if has_aliases:
            groups_with_aliases += 1
        if has_examples:
            groups_with_examples += 1

        log.info(
            "Group '%s': columns=%s, aliases=%s, examples=%s, tokens=%s",
            name,
            "loaded" if has_columns else "unavailable",
            has_aliases,
            has_examples,
            chunk["token_count"],
        )
        chunks.append(chunk)

    chunks, skipped_count = await _filter_chunks_needing_upsert(chunks, pool)
    if not chunks:
        return {
            "inserted_count": 0,
            "updated_count": 0,
            "skipped_count": skipped_count,
            "failed_groups": failed_groups,
            "enrichment_summary": {
                "groups_with_columns": groups_with_columns,
                "groups_without_columns": groups_without_columns,
                "groups_with_aliases": groups_with_aliases,
                "groups_with_examples": groups_with_examples,
            },
        }

    embeddings = await embed.embed_texts([c["text"] for c in chunks])

    inserted_count = 0
    updated_count = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            for chunk, embedding in zip(chunks, embeddings):
                metadata = {
                    k: v
                    for k, v in chunk.items()
                    if k not in {"text", "source", "chunk_index", "token_count", "embedding_model"}
                }
                row = await conn.fetchrow(
                    _GROUP_UPSERT_SQL,
                    chunk["text"],
                    embedding,
                    chunk["source"],
                    chunk["chunk_index"],
                    chunk["token_count"],
                    chunk["embedding_model"],
                    json.dumps(metadata),
                )
                if row is None:
                    continue
                if bool(row["inserted"]):
                    inserted_count += 1
                elif bool(row["updated"]):
                    updated_count += 1

    return {
        "inserted_count": inserted_count,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "failed_groups": failed_groups,
        "enrichment_summary": {
            "groups_with_columns": groups_with_columns,
            "groups_without_columns": groups_without_columns,
            "groups_with_aliases": groups_with_aliases,
            "groups_with_examples": groups_with_examples,
        },
    }


async def _upsert_versioned_chunks(
    chunks: list[dict],
    pool: asyncpg.Pool,
) -> dict[str, int]:
    chunks, skipped_count = await _filter_chunks_needing_upsert(chunks, pool)
    if not chunks:
        return {"inserted_count": 0, "updated_count": 0, "skipped_count": skipped_count}

    embeddings = await embed.embed_texts([c["text"] for c in chunks])

    inserted_count = 0
    updated_count = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            for chunk, embedding in zip(chunks, embeddings):
                metadata = {
                    k: v
                    for k, v in chunk.items()
                    if k not in {"text", "source", "chunk_index", "token_count", "embedding_model"}
                }
                row = await conn.fetchrow(
                    _KNOWLEDGE_UPSERT_SQL,
                    chunk["text"],
                    embedding,
                    chunk["source"],
                    chunk["chunk_index"],
                    chunk["token_count"],
                    chunk["embedding_model"],
                    json.dumps(metadata),
                )
                if row is None:
                    continue
                if bool(row["inserted"]):
                    inserted_count += 1
                elif bool(row["updated"]):
                    updated_count += 1

    return {
        "inserted_count": inserted_count,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
    }


async def ingest_enriched_knowledge(
    include_column_catalog: bool,
    include_sql_examples: bool,
    include_relations: bool,
    include_graph: bool,
    include_view_registry: bool,
    include_onboarding_rules: bool,
    column_limit: int | None,
    sql_example_limit: int | None,
    relation_limit: int | None,
    graph_limit: int | None,
    view_registry_limit: int | None,
    pool: asyncpg.Pool,
) -> dict[str, int]:
    chunks: list[dict] = []

    if include_column_catalog:
        column_chunks = await schema_loader.load_live_column_catalog_chunks(
            chunker.settings,
            limit=column_limit,
        )
        if not column_chunks:
            column_chunks = schema_loader.load_column_catalog_chunks(limit=column_limit)
        for chunk in column_chunks:
            chunk["token_count"] = chunker.count_tokens(chunk["text"])
            chunk["embedding_model"] = chunker.settings.embedding_model
        chunks.extend(column_chunks)
        log.info("Prepared %d column-catalog chunks", len(column_chunks))

    if include_sql_examples:
        sql_chunks = schema_loader.load_sql_example_chunks(limit=sql_example_limit)
        for chunk in sql_chunks:
            chunk["token_count"] = chunker.count_tokens(chunk["text"])
            chunk["embedding_model"] = chunker.settings.embedding_model
        chunks.extend(sql_chunks)
        log.info("Prepared %d SQL-example chunks", len(sql_chunks))

    if include_relations:
        relation_chunks = schema_loader.load_relation_chunks(limit=relation_limit)
        for chunk in relation_chunks:
            chunk["token_count"] = chunker.count_tokens(chunk["text"])
            chunk["embedding_model"] = chunker.settings.embedding_model
        chunks.extend(relation_chunks)
        log.info("Prepared %d relation-link chunks", len(relation_chunks))

    if include_graph:
        graph_chunks = schema_loader.load_table_graph_chunks(limit=graph_limit)
        for chunk in graph_chunks:
            chunk["token_count"] = chunker.count_tokens(chunk["text"])
            chunk["embedding_model"] = chunker.settings.embedding_model
        chunks.extend(graph_chunks)
        log.info("Prepared %d table-node chunks", len(graph_chunks))

    if include_view_registry:
        view_chunks = schema_loader.load_view_registry_chunks(limit=view_registry_limit)
        for chunk in view_chunks:
            chunk["token_count"] = chunker.count_tokens(chunk["text"])
            chunk["embedding_model"] = chunker.settings.embedding_model
        chunks.extend(view_chunks)
        log.info("Prepared %d view-node chunks", len(view_chunks))

    if include_onboarding_rules:
        rule_chunks = schema_loader.load_onboarding_rules_chunk()
        for chunk in rule_chunks:
            chunk["token_count"] = chunker.count_tokens(chunk["text"])
            chunk["embedding_model"] = chunker.settings.embedding_model
        chunks.extend(rule_chunks)
        log.info("Prepared %d schema-rule chunks", len(rule_chunks))

    if not chunks:
        return {"inserted_count": 0, "updated_count": 0, "skipped_count": 0}

    return await _upsert_versioned_chunks(chunks, pool)
