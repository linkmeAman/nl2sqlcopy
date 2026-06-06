from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from nl2sql_service.column_loader import load_column_catalog
from nl2sql_service.config import Settings
from nl2sql_service.synonym_map import aliases_for_column_introspection


def _rag_schema_dir() -> Path:
    configured = os.getenv("RAG_SCHEMA_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).parent.parent / "rag_schema"


def _workspace_root() -> Path:
    return Path(__file__).parent.parent


def _docs_dir() -> Path:
    configured = os.getenv("NL2SQL_DOCS_DIR")
    if configured:
        return Path(configured)
    return _workspace_root() / "ignore_docs_now" / "docs"


def _list_json_files(path: Path) -> list[Path]:
    return sorted(path.glob("*.json"), key=lambda p: p.name)


def _iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            yield line


def load_entities() -> list[dict]:
    entities_dir = _rag_schema_dir() / "entities"
    entities: list[dict] = []
    for file_path in _list_json_files(entities_dir):
        with file_path.open("r", encoding="utf-8") as handle:
            entities.append(json.load(handle))
    return sorted(entities, key=lambda entity: str(entity.get("entity_id", "")))


def load_relations() -> dict[str, dict]:
    relations_dir = _rag_schema_dir() / "relations"
    relations: dict[str, dict] = {}
    for file_path in _list_json_files(relations_dir):
        with file_path.open("r", encoding="utf-8") as handle:
            relation = json.load(handle)
        relation_id = str(relation.get("relation_id", ""))
        if not relation_id:
            continue
        relations[relation_id] = relation
    return relations


def get_relations_for_entity(entity_id: str) -> list[dict]:
    matched: list[dict] = []
    for relation in load_relations().values():
        candidate_groups = relation.get("candidate_entity_groups", [])
        if entity_id not in candidate_groups:
            continue
        if bool(relation.get("risky_needs_review", False)):
            continue
        if not bool(relation.get("use_for_chunk_expansion_by_default", False)):
            continue
        matched.append(relation)
    return matched


def load_classifications() -> dict[str, str]:
    path = _rag_schema_dir() / "graph" / "table_classification.json"
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}

    mapped: dict[str, str] = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            table = str(item.get("table_name") or item.get("table") or "")
            classification = str(
                item.get("classification_type")
                or item.get("classification")
                or item.get("type")
                or ""
            )
            if table:
                mapped[table] = classification
    return mapped


def load_chunking_rules() -> dict:
    path = _rag_schema_dir() / "rules" / "chunking_rules.json"
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_entity(entity_id: str) -> dict | None:
    for entity in load_entities():
        if entity.get("entity_id") == entity_id:
            return entity
    return None


def get_business_aliases(entity_id: str) -> dict[str, list[str]]:
    entity = get_entity(entity_id)
    if entity is None:
        return {}

    raw_aliases = entity.get("business_aliases", {})
    if not isinstance(raw_aliases, dict):
        return {}

    aliases: dict[str, list[str]] = {}
    for key, terms in raw_aliases.items():
        if not isinstance(terms, list):
            continue
        clean_terms = [str(term) for term in terms if str(term).strip()]
        if clean_terms:
            aliases[str(key)] = clean_terms
    return aliases


def get_example_questions(entity_id: str) -> list[str]:
    entity = get_entity(entity_id)
    if entity is None:
        return []

    raw_questions = entity.get("example_questions", [])
    if not isinstance(raw_questions, list):
        return []
    return [str(question) for question in raw_questions if str(question).strip()]


def get_all_group_names() -> list[str]:
    return [str(entity["entity_id"]) for entity in load_entities()]


def get_schema_version(entity_id: str) -> str:
    entities_dir = _rag_schema_dir() / "entities"
    for file_path in _list_json_files(entities_dir):
        file_bytes = file_path.read_bytes()
        entity = json.loads(file_bytes.decode("utf-8"))
        if entity.get("entity_id") == entity_id:
            return hashlib.md5(file_bytes).hexdigest()[:8]
    raise KeyError(f"Entity '{entity_id}' not found in rag_schema/entities/")


def load_column_catalog_chunks(limit: int | None = None) -> list[dict]:
    docs = _docs_dir()
    paths = [
        docs / "nl2sql-columns.jsonl",
        docs / "generated" / "nl2sql_schema_tables.jsonl",
        docs / "generated" / "nl2sql_schema_views.jsonl",
    ]

    chunks: list[dict] = []
    seen_sources: set[str] = set()

    for path in paths:
        if not path.exists():
            continue

        for line in _iter_jsonl(path):
            record = json.loads(line)
            text = str(record.get("text", "")).strip()
            if not text:
                continue

            record_id = str(record.get("id") or "")
            if not record_id:
                continue
            source = f"column_catalog::{record_id}"
            if source in seen_sources:
                continue
            seen_sources.add(source)

            tags = record.get("tags", [])
            if not isinstance(tags, list):
                tags = []

            chunks.append(
                {
                    "text": text,
                    "source": source,
                    "chunk_index": 0,
                    "schema_version": hashlib.md5(line.encode("utf-8")).hexdigest()[:8],
                    "type": "column_catalog",
                    "record_id": record_id,
                    "title": str(record.get("title", "")),
                    "tags": [str(tag) for tag in tags],
                    "object_type": str(record.get("object_type", "")),
                    "database": str(record.get("database", "")),
                    "object_name": str(record.get("object_name", "")),
                    "full_object_name": str(record.get("full_object_name", "")),
                    "source_kind": str(record.get("source_kind", "")),
                }
            )

            if limit is not None and len(chunks) >= limit:
                return chunks

    return chunks


async def load_live_column_catalog_chunks(
    settings: Settings,
    limit: int | None = None,
) -> list[dict]:
    records = await load_column_catalog(settings, tables=None)
    chunks: list[dict] = []

    for record in records:
        table_name = str(record.get("table_name") or "").strip().lower()
        column_name = str(record.get("column_name") or "").strip().lower()
        if not table_name or not column_name:
            continue

        aliases = aliases_for_column_introspection(column_name)
        search_terms = [column_name.replace("_", " "), *aliases]
        data_type = str(record.get("data_type") or "").strip().lower()
        payload = {
            "table_name": table_name,
            "column_name": column_name,
            "data_type": data_type,
            "ordinal_position": int(record.get("ordinal_position") or 0),
            "aliases": aliases,
        }
        text_lines = [
            f"Table: {table_name}",
            f"Column: {column_name}",
            f"Semantic aliases: {', '.join(aliases)}" if aliases else "Semantic aliases: none",
            f"Data type: {data_type}" if data_type else "Data type: unknown",
            f"Search terms: {', '.join(search_terms)}",
        ]

        chunks.append(
            {
                "text": "\n".join(text_lines),
                "source": f"column_catalog::{table_name}::{column_name}",
                "chunk_index": 0,
                "schema_version": hashlib.md5(
                    json.dumps(payload, sort_keys=True).encode("utf-8")
                ).hexdigest()[:8],
                "type": "column_catalog",
                "record_id": f"{table_name}.{column_name}",
                "title": f"{table_name}.{column_name} column",
                "tags": ["columns", table_name],
                "object_type": "column",
                "database": "",
                "object_name": table_name,
                "table_name": table_name,
                "column_name": column_name,
                "column_aliases": aliases,
                "data_type": data_type,
                "full_object_name": f"{table_name}.{column_name}",
                "source_kind": "mysql_introspection",
            }
        )

        if limit is not None and len(chunks) >= limit:
            return chunks

    return chunks


def load_relation_chunks(limit: int | None = None) -> list[dict]:
    relations_dir = _rag_schema_dir() / "relations"
    chunks: list[dict] = []
    for file_path in _list_json_files(relations_dir):
        file_bytes = file_path.read_bytes()
        relation = json.loads(file_bytes.decode("utf-8"))

        relation_id = str(relation.get("relation_id", ""))
        if not relation_id:
            continue

        source_table = str(relation.get("source_table", ""))
        target_table = str(relation.get("target_table", ""))
        source_cols = ", ".join(relation.get("source_columns", []))
        target_cols = ", ".join(relation.get("target_columns", []))
        join_expr = str(relation.get("join_expression", ""))
        rel_type = str(relation.get("relationship_type", ""))
        confidence = str(relation.get("confidence", ""))
        reasoning = str(relation.get("reasoning", ""))
        entities = relation.get("candidate_entity_groups", [])
        entities_str = ", ".join(entities) if entities else ""

        text_parts = [
            f"Relation: {source_table}.{source_cols} → {target_table}.{target_cols}",
            f"Join SQL: {join_expr}",
            f"Relationship: {rel_type}, confidence: {confidence}",
        ]
        if entities_str:
            text_parts.append(f"Entities: {entities_str}")
        if reasoning:
            text_parts.append(f"Reasoning: {reasoning}")

        chunks.append(
            {
                "text": "\n".join(text_parts),
                "source": f"relation::{relation_id}",
                "chunk_index": 0,
                "schema_version": hashlib.md5(file_bytes).hexdigest()[:8],
                "type": "relation_link",
                "relation_id": relation_id,
                "source_table": source_table,
                "target_table": target_table,
                "relationship_type": rel_type,
                "confidence": confidence,
                "candidate_entity_groups": entities,
            }
        )

        if limit is not None and len(chunks) >= limit:
            return chunks

    return chunks


def load_table_graph_chunks(limit: int | None = None) -> list[dict]:
    path = _rag_schema_dir() / "graph" / "table_graph.json"
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    nodes = data.get("nodes", data) if isinstance(data, dict) else data
    file_bytes = path.read_bytes()
    file_hash = hashlib.md5(file_bytes).hexdigest()[:8]

    chunks: list[dict] = []
    for idx, node in enumerate(nodes):
        table_name = str(node.get("table_name", ""))
        if not table_name:
            continue
        schema_name = str(node.get("schema_name", ""))
        classification = str(node.get("classification", ""))
        cluster = str(node.get("cluster", ""))
        node_kind = str(node.get("node_kind", "table"))
        is_root = bool(node.get("is_root_candidate", False))

        text = (
            f"Table: {table_name} (schema: {schema_name})\n"
            f"Kind: {node_kind}, Classification: {classification}, Cluster: {cluster}\n"
            f"Is root candidate: {is_root}"
        )

        chunks.append(
            {
                "text": text,
                "source": f"table_graph::{table_name}",
                "chunk_index": idx,
                "schema_version": file_hash,
                "type": "table_node",
                "table_name": table_name,
                "schema_name": schema_name,
                "classification": classification,
                "cluster": cluster,
                "is_root_candidate": is_root,
            }
        )

        if limit is not None and len(chunks) >= limit:
            return chunks

    return chunks


def load_view_registry_chunks(limit: int | None = None) -> list[dict]:
    path = _rag_schema_dir() / "graph" / "view_registry.json"
    with path.open("r", encoding="utf-8") as handle:
        entries = json.load(handle)

    file_bytes = path.read_bytes()
    file_hash = hashlib.md5(file_bytes).hexdigest()[:8]

    chunks: list[dict] = []
    for idx, entry in enumerate(entries):
        view_name = str(entry.get("view_name", ""))
        if not view_name:
            continue
        schema_name = str(entry.get("schema_name", ""))
        role = str(entry.get("recommended_role", ""))
        derived_from = entry.get("derived_from_tables", [])
        derived_str = ", ".join(derived_from) if derived_from else ""

        text_parts = [
            f"View: {view_name} (schema: {schema_name})",
            f"Role: {role}",
        ]
        if derived_str:
            text_parts.append(f"Derived from tables: {derived_str}")

        chunks.append(
            {
                "text": "\n".join(text_parts),
                "source": f"view_registry::{view_name}",
                "chunk_index": idx,
                "schema_version": file_hash,
                "type": "view_node",
                "view_name": view_name,
                "schema_name": schema_name,
                "recommended_role": role,
                "derived_from_tables": derived_from,
            }
        )

        if limit is not None and len(chunks) >= limit:
            return chunks

    return chunks


def load_onboarding_rules_chunk() -> list[dict]:
    path = _rag_schema_dir() / "rules" / "onboarding_rules.json"
    if not path.exists():
        return []
    file_bytes = path.read_bytes()
    data = json.loads(file_bytes.decode("utf-8"))

    sections: list[str] = ["Schema onboarding rules:"]
    for key, rules in data.items():
        label = key.replace("_", " ").capitalize()
        sections.append(f"\n{label}:")
        if isinstance(rules, list):
            for rule in rules:
                sections.append(f"  - {rule}")
        else:
            sections.append(f"  {rules}")

    return [
        {
            "text": "\n".join(sections),
            "source": "onboarding_rules::schema_rules",
            "chunk_index": 0,
            "schema_version": hashlib.md5(file_bytes).hexdigest()[:8],
            "type": "schema_rule",
        }
    ]


def load_sql_example_chunks(limit: int | None = 200) -> list[dict]:
    schema_export_path = _docs_dir() / "mysql_schema_export.txt"
    if not schema_export_path.exists():
        return []

    # CREATE ... VIEW `view_name` AS <select...>;
    create_view_regex = re.compile(r"VIEW `([^`]+)` AS (select .*);$", re.IGNORECASE)

    chunks: list[dict] = []
    seen_sources: set[str] = set()

    with schema_export_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if " VIEW `" not in line or " as select " not in line.lower():
                continue

            match = create_view_regex.search(line)
            if not match:
                continue

            view_name = match.group(1)
            select_sql = match.group(2)
            if len(select_sql) > 8000:
                select_sql = select_sql[:8000] + " ..."

            source = f"sql_example::{view_name}"
            if source in seen_sources:
                continue
            seen_sources.add(source)

            chunks.append(
                {
                    "text": f"SQL example from view {view_name}\nSQL:\n{select_sql}",
                    "source": source,
                    "chunk_index": 0,
                    "schema_version": hashlib.md5(raw_line.encode("utf-8")).hexdigest()[:8],
                    "type": "sql_example",
                    "view_name": view_name,
                    "full_object_name": view_name,
                    "object_type": "view",
                    "source_kind": "mysql_schema_export",
                }
            )

            if limit is not None and len(chunks) >= limit:
                return chunks

    return chunks


def validate_loader() -> None:
    rag_schema_dir = _rag_schema_dir()
    if not rag_schema_dir.exists():
        raise RuntimeError(f"Missing required path: {rag_schema_dir}")
    if not rag_schema_dir.is_dir():
        raise RuntimeError(f"Path is not a directory: {rag_schema_dir}")

    entities_dir = rag_schema_dir / "entities"
    if not entities_dir.exists():
        raise RuntimeError(f"Missing required path: {entities_dir}")
    if not entities_dir.is_dir():
        raise RuntimeError(f"Path is not a directory: {entities_dir}")

    entity_files = _list_json_files(entities_dir)
    if not entity_files:
        raise RuntimeError(f"Missing required path: {entities_dir}/*.json")

    relations_dir = rag_schema_dir / "relations"
    if not relations_dir.exists():
        raise RuntimeError(f"Missing required path: {relations_dir}")
    if not relations_dir.is_dir():
        raise RuntimeError(f"Path is not a directory: {relations_dir}")

    classifications_path = rag_schema_dir / "graph" / "table_classification.json"
    if not classifications_path.exists():
        raise RuntimeError(f"Missing required path: {classifications_path}")

    docs_path = _docs_dir()
    if not docs_path.exists():
        raise RuntimeError(f"Missing required path: {docs_path}")
    if not docs_path.is_dir():
        raise RuntimeError(f"Path is not a directory: {docs_path}")


def loader_readiness() -> dict[str, object]:
    rag_schema_dir = _rag_schema_dir()
    docs_dir = _docs_dir()
    required_docs = [
        docs_dir / "nl2sql-columns.jsonl",
        docs_dir / "generated" / "nl2sql_schema_tables.jsonl",
        docs_dir / "generated" / "nl2sql_schema_views.jsonl",
        docs_dir / "mysql_schema_export.txt",
    ]
    issues: list[dict[str, str]] = []

    try:
        validate_loader()
    except RuntimeError as exc:
        issues.append({"code": "SCHEMA_LOADER_INVALID", "message": str(exc)})

    missing_docs = [str(path) for path in required_docs if not path.exists()]
    if missing_docs:
        issues.append(
            {
                "code": "SCHEMA_DOCS_MISSING",
                "message": "Missing required NL2SQL docs assets.",
            }
        )

    entity_count = 0
    relation_count = 0
    if not issues:
        entity_count = len(load_entities())
        relation_count = len(load_relations())

    return {
        "status": "ok" if not issues else "error",
        "rag_schema_dir": str(rag_schema_dir),
        "docs_dir": str(docs_dir),
        "entity_count": entity_count,
        "relation_count": relation_count,
        "missing_docs": missing_docs,
        "issues": issues,
    }
