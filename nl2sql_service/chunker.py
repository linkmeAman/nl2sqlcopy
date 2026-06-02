from __future__ import annotations

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter
from typing import Any

from nl2sql_service.config import settings
from nl2sql_service.models import SchemaTable
from nl2sql_service import schema_loader
from nl2sql_service.column_loader import load_columns_for_tables
from nl2sql_service.config import Settings

Chunk = dict[str, Any]

_ENCODING_UNAVAILABLE = object()
_encoding = None


def count_tokens(text: str) -> int:
    """Return the number of cl100k_base tokens in *text*."""
    global _encoding
    if _encoding is _ENCODING_UNAVAILABLE:
        return max(1, len(text.split()))
    if _encoding is None:
        try:
            _encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _encoding = _ENCODING_UNAVAILABLE
            return max(1, len(text.split()))
    return len(_encoding.encode(text))


# ---------------------------------------------------------------------------
# Path 1: free-text chunking
# ---------------------------------------------------------------------------


def chunk_text(text: str, source: str) -> list[dict]:
    """
    Split *text* into overlapping token-bounded chunks.

    Each returned dict contains the fixed metadata keys required by
    ``db.insert_chunks``:  ``text``, ``source``, ``chunk_index``,
    ``token_count``, ``embedding_model``.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=512,
        chunk_overlap=64,
        length_function=count_tokens,
    )
    raw_chunks: list[str] = splitter.split_text(text)

    return [
        {
            "text": chunk,
            "source": source,
            "chunk_index": i,
            "token_count": count_tokens(chunk),
            "embedding_model": settings.embedding_model,
        }
        for i, chunk in enumerate(raw_chunks)
    ]


# ---------------------------------------------------------------------------
# Path 2: schema chunking — one chunk per table
# ---------------------------------------------------------------------------


def chunk_schema(tables: list[SchemaTable], source: str) -> list[dict]:
    """
    Produce one chunk per :class:`SchemaTable`, using the pre-rendered
    ``text`` field verbatim.

    All schema metadata fields are included so they can be stored in the
    ``metadata`` JSONB column alongside the fixed columns.
    """
    chunks: list[dict] = []
    for i, table in enumerate(tables):
        chunks.append(
            {
                "text": table.text,
                "source": source,
                "chunk_index": i,
                "token_count": count_tokens(table.text),
                "embedding_model": settings.embedding_model,
                # Schema-specific fields stored in metadata
                "database": table.database,
                "object_name": table.object_name,
                "object_type": table.object_type,
                "full_object_name": table.full_object_name,
                "column_count": table.column_count,
                "source_kind": table.source_kind,
                "total_chunks": table.total_chunks,
            }
        )
    return chunks


# ---------------------------------------------------------------------------
# Path 3: schema-group chunking — one chunk per semantic group
# ---------------------------------------------------------------------------


async def chunk_schema_group(
    group_name: str,
    settings: Settings,
    allowed_columns: dict[str, list[str]] | None = None,
) -> Chunk:
    """
    Produce a single embeddable chunk dict from a rag_schema entity.
    """
    entity = schema_loader.get_entity(group_name)
    if entity is None:
        raise ValueError(f"Entity '{group_name}' not found in rag_schema/entities/")

    included_tables = [str(table) for table in entity.get("included_tables", [])]
    summarized_tables = [str(table) for table in entity.get("summarized_tables", [])]
    referenced_tables = [str(table) for table in entity.get("referenced_tables", [])]
    root_table = str(entity["root_table"])

    tables = [root_table] + included_tables
    related_tables = summarized_tables + referenced_tables

    if allowed_columns is None:
        try:
            allowed_columns = await load_columns_for_tables(tables=tables, settings=settings)
        except Exception:  # noqa: BLE001
            allowed_columns = {}

    aliases = schema_loader.get_business_aliases(group_name)
    examples = schema_loader.get_example_questions(group_name)

    lines = [
        f"Group: {entity['chunk_group_name']}",
        f"Root table: {root_table}",
        f"Included tables: {', '.join(included_tables)}",
        f"Summarized tables: {', '.join(summarized_tables)}",
        f"Referenced tables: {', '.join(referenced_tables)}",
        "Columns:",
    ]

    for table in tables:
        cols = allowed_columns.get(table.lower(), []) if allowed_columns else []
        if cols:
            lines.append(f"  {table}: {', '.join(cols)}")
        else:
            lines.append(f"  {table}: (columns unavailable)")

    if aliases:
        lines.append("Business terms:")
        for table, terms in aliases.items():
            lines.append(f"  {table}: also called {', '.join(terms)}")

    relation_ids = [str(rel_id) for rel_id in entity.get("relation_ids", [])]
    lines.append(f"Relations: {'; '.join(relation_ids)}")

    if examples:
        lines.append("Example questions:")
        for question in examples:
            lines.append(f"  - {question}")

    lines.append(f"Description: {entity.get('rationale', '')}")
    text = "\n".join(lines)

    estimated = int(len(text.split()) * 1.3)
    if estimated > 400:
        raise ValueError(
            f"Group '{group_name}' estimated {estimated} tokens "
            f"after enrichment. Exceeds 400 limit. "
            f"Reduce example_questions or alias count in "
            f"rag_schema entity file."
        )

    # schema_version tracks entity file changes only.
    # To force re-embed after MySQL column changes,
    # touch the entity file or bump its content.
    schema_version = schema_loader.get_schema_version(group_name)
    source = entity["chunk_group_name"]
    has_columns = bool(allowed_columns and any(allowed_columns.values()))
    has_aliases = bool(aliases)
    has_examples = bool(examples)

    metadata = {
        "type": "schema_group",
        "entity_id": entity["entity_id"],
        "chunk_group_name": entity["chunk_group_name"],
        "root_table": entity["root_table"],
        "root_table_ref": entity.get("root_table_ref", ""),
        "tables": tables,
        "related_tables": related_tables,
        "group_description": entity.get("rationale", ""),
        "schema_version": schema_version,
        "secondary_memberships": entity.get("secondary_memberships", []),
        "table_ref_map": entity.get("table_ref_map", {}),
        "has_columns": has_columns,
        "has_aliases": has_aliases,
        "has_examples": has_examples,
        "column_source": "mysql_live" if has_columns else "unavailable",
    }

    return {
        "text": text,
        "source": source,
        "chunk_index": 0,
        "token_count": estimated,
        "embedding_model": settings.embedding_model,
        **metadata,
    }
