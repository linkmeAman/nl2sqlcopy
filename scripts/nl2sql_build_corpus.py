import argparse
import json
import re
from collections import Counter
from pathlib import Path


DEFAULT_SCHEMA_EXPORT = "docs/mysql_schema_export.txt"
DEFAULT_RELATIONS_DOC = "docs/database-table-relations.md"
DEFAULT_BUSINESS_CONTEXT_DOC = "docs/vector-db-schema-business-context.md"
DEFAULT_OUTPUT_DIR = "docs/generated"
MAX_COLUMNS_PER_CHUNK = 80


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build generated NL2SQL corpus files from schema and relation sources."
    )
    parser.add_argument("--schema-export", default=DEFAULT_SCHEMA_EXPORT)
    parser.add_argument("--relations-doc", default=DEFAULT_RELATIONS_DOC)
    parser.add_argument("--business-context-doc", default=DEFAULT_BUSINESS_CONTEXT_DOC)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "item"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    path.write_text(payload + ("\n" if rows else ""), encoding="utf-8")


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def extract_databases(schema_text: str) -> dict[str, str]:
    pattern = re.compile(
        r"################################################################################\nDATABASE:\s+([^\n]+)\n################################################################################\n(.*?)(?=################################################################################\nDATABASE:|\Z)",
        re.DOTALL,
    )
    return {match.group(1).strip(): match.group(2) for match in pattern.finditer(schema_text)}


def extract_views(database: str, db_text: str) -> list[dict]:
    pattern = re.compile(
        r"-+\nVIEW:\s+([^\n]+)\n-+\n(CREATE .*?;)\n",
        re.DOTALL,
    )
    views = []
    for match in pattern.finditer(db_text):
        full_name = match.group(1).strip()
        statement = match.group(2).strip()
        simple_name = full_name.split(".")[-1]
        columns = unique_preserve_order(
            re.findall(r"\sAS\s+`([^`]+)`", statement, flags=re.IGNORECASE)
        )
        joined_objects = unique_preserve_order(
            [
                part.split(".")[-1].replace("`", "")
                for part in re.findall(
                    r"\b(?:FROM|JOIN)\s+((?:`[^`]+`|\w+)(?:\.(?:`[^`]+`|\w+)){0,2})",
                    statement,
                    flags=re.IGNORECASE,
                )
            ]
        )
        views.append(
            {
                "database": database,
                "name": simple_name,
                "full_name": full_name,
                "columns": columns,
                "depends_on": joined_objects,
                "statement": statement,
            }
        )
    return views


def extract_tables(database: str, db_text: str) -> list[dict]:
    pattern = re.compile(
        r"-+\nBASE TABLE:\s+([^\n]+)\n-+\n(CREATE TABLE `([^`]+)`\s*\((.*?)\)\s*ENGINE=.*?)\n\n",
        re.DOTALL,
    )
    tables = []
    for match in pattern.finditer(db_text):
        full_name = match.group(1).strip()
        simple_name = match.group(3).strip()
        body = match.group(4)
        statement = match.group(2).strip()
        columns = unique_preserve_order(re.findall(r"^\s*`([^`]+)`\s", body, flags=re.MULTILINE))
        indexes = unique_preserve_order(re.findall(r"^\s*(?:UNIQUE\s+)?KEY\s+`?([^`(\s]+)`?", body, flags=re.MULTILINE))
        foreign_keys = []
        for fk_match in re.finditer(
            r"CONSTRAINT\s+`([^`]+)`\s+FOREIGN KEY\s+\(`([^`]+)`\)\s+REFERENCES\s+`([^`]+)`\s+\(`([^`]+)`\)",
            body,
            flags=re.IGNORECASE,
        ):
            foreign_keys.append(
                {
                    "constraint": fk_match.group(1),
                    "column": fk_match.group(2),
                    "target_table": fk_match.group(3),
                    "target_column": fk_match.group(4),
                }
            )
        tables.append(
            {
                "database": database,
                "name": simple_name,
                "full_name": full_name,
                "columns": columns,
                "indexes": indexes,
                "foreign_keys": foreign_keys,
                "statement": statement,
            }
        )
    return tables


def unique_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def relation_evidence_counts(relations_text: str) -> Counter:
    counter: Counter[str] = Counter()
    for confidence in re.findall(r"`Confidence:\s+([^`]+)`", relations_text):
        counter[confidence.strip()] += 1
    return counter


def extract_relation_entries(relations_text: str) -> list[dict]:
    pattern = re.compile(
        r"- `([^`]+)`\n\s+`Cardinality:\s+([^`]+)`\n\s+`Confidence:\s+([^`]+)`\n\s+Evidence:\s+([^\n]+)(?:\n\s+Note:\s+([^\n]+))?",
        re.MULTILINE,
    )
    entries = []
    for match in pattern.finditer(relations_text):
        relation = match.group(1).strip()
        source = relation
        target = ""
        if "->" in relation:
            source, target = [part.strip() for part in relation.split("->", 1)]
        entries.append(
            {
                "relation": relation,
                "source": source,
                "target": target,
                "cardinality": match.group(2).strip(),
                "confidence": match.group(3).strip(),
                "evidence": [item.strip() for item in match.group(4).split(",")],
                "note": (match.group(5) or "").strip(),
            }
        )
    return entries


def build_table_rows(tables: list[dict]) -> list[dict]:
    rows = []
    for table in tables:
        foreign_key_text = "none"
        if table["foreign_keys"]:
            foreign_key_text = "; ".join(
                f"{item['column']} -> {item['target_table']}.{item['target_column']}"
                for item in table["foreign_keys"]
            )
        column_chunks = chunked(table["columns"], MAX_COLUMNS_PER_CHUNK)
        for chunk_index, columns_chunk in enumerate(column_chunks, start=1):
            rows.append(
                {
                    "id": f"table_{slugify(table['database'])}_{slugify(table['name'])}_chunk_{chunk_index}",
                    "title": f"Table {table['database']}.{table['name']} columns {chunk_index}",
                    "tags": ["generated", "schema", "table", table["database"], table["name"]],
                    "text": (
                        f"Base table {table['database']}.{table['name']}. "
                        f"Columns: {', '.join(columns_chunk)}. "
                        f"Indexes: {', '.join(table['indexes']) if table['indexes'] else 'none'}. "
                        f"Foreign keys: {foreign_key_text}."
                    ),
                    "object_type": "table",
                    "database": table["database"],
                    "object_name": table["name"],
                    "full_object_name": f"{table['database']}.{table['name']}",
                    "chunk_index": chunk_index,
                    "total_chunks": len(column_chunks),
                    "column_count": len(table["columns"]),
                    "source_kind": "schema_export",
                }
            )
    return rows


def build_view_rows(views: list[dict]) -> list[dict]:
    rows = []
    for view in views:
        depends_on = ", ".join(view["depends_on"]) if view["depends_on"] else "none"
        column_chunks = chunked(view["columns"], MAX_COLUMNS_PER_CHUNK) or [[]]
        for chunk_index, columns_chunk in enumerate(column_chunks, start=1):
            rows.append(
                {
                    "id": f"view_{slugify(view['database'])}_{slugify(view['name'])}_chunk_{chunk_index}",
                    "title": f"View {view['database']}.{view['name']} columns {chunk_index}",
                    "tags": ["generated", "schema", "view", view["database"], view["name"]],
                    "text": (
                        f"View {view['database']}.{view['name']}. "
                        f"Columns: {', '.join(columns_chunk) if columns_chunk else 'no explicit aliases parsed'}. "
                        f"Depends on: {depends_on}."
                    ),
                    "object_type": "view",
                    "database": view["database"],
                    "object_name": view["name"],
                    "full_object_name": f"{view['database']}.{view['name']}",
                    "chunk_index": chunk_index,
                    "total_chunks": len(column_chunks),
                    "column_count": len(view["columns"]),
                    "dependency_count": len(view["depends_on"]),
                    "source_kind": "schema_export",
                }
            )
    return rows


def build_relationship_rows(entries: list[dict]) -> list[dict]:
    rows = []
    for entry in entries:
        tags = ["generated", "relationship", entry["confidence"].lower().replace(" ", "_")]
        if entry["source"]:
            tags.append(slugify(entry["source"].split(".")[0]))
        rows.append(
            {
                "id": f"relationship_{slugify(entry['relation'])}",
                "title": f"Relationship {entry['relation']}",
                "tags": tags,
                "text": (
                    f"Relationship: {entry['relation']}. "
                    f"Cardinality: {entry['cardinality']}. "
                    f"Confidence: {entry['confidence']}. "
                    f"Evidence: {', '.join(entry['evidence'])}."
                    + (f" Note: {entry['note']}." if entry['note'] else "")
                ),
                "object_type": "relationship",
                "source_object": entry["source"],
                "target_object": entry["target"],
                "confidence": entry["confidence"],
                "cardinality": entry["cardinality"],
                "source_kind": "relations_doc",
            }
        )
    return rows


def build_business_rule_rows(business_context_text: str, relation_counts: Counter) -> list[dict]:
    paragraphs = [paragraph.strip() for paragraph in business_context_text.split("\n\n") if paragraph.strip()]
    rows = []
    summary_text = (
        "Generated corpus summary. "
        f"Relationship evidence counts: {', '.join(f'{key}={value}' for key, value in sorted(relation_counts.items()))}. "
        "Prefer enriched views for business questions, treat polymorphic references carefully, and validate SQL against the schema export before execution."
    )
    rows.append(
        {
            "id": "generated_corpus_summary",
            "title": "Generated Corpus Summary",
            "tags": ["generated", "summary", "business_rules"],
            "text": summary_text,
            "object_type": "summary",
            "source_kind": "derived",
        }
    )
    for index, paragraph in enumerate(paragraphs, start=1):
        first_sentence = paragraph.split(". ", 1)[0].strip()
        rows.append(
            {
                "id": f"business_context_chunk_{index}",
                "title": first_sentence[:100] or f"Business context {index}",
                "tags": ["generated", "business_context", "narrative"],
                "text": paragraph,
                "object_type": "business_context",
                "source_kind": "business_context_doc",
            }
        )
    return rows


def build_manifest(output_dir: Path, tables: list[dict], views: list[dict], relationships: list[dict]) -> dict:
    table_databases = Counter(table["database"] for table in tables)
    view_databases = Counter(view["database"] for view in views)
    return {
        "output_dir": str(output_dir),
        "table_count": len(tables),
        "view_count": len(views),
        "relationship_count": len(relationships),
        "databases": {
            "tables": dict(sorted(table_databases.items())),
            "views": dict(sorted(view_databases.items())),
        },
        "files": {
            "tables": "nl2sql_schema_tables.jsonl",
            "views": "nl2sql_schema_views.jsonl",
            "relationships": "nl2sql_relationships.jsonl",
            "business_rules": "nl2sql_business_rules.jsonl",
        },
    }


def main() -> None:
    args = parse_args()
    schema_export_path = Path(args.schema_export)
    relations_doc_path = Path(args.relations_doc)
    business_context_doc_path = Path(args.business_context_doc)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    schema_text = read_text(schema_export_path)
    relations_text = read_text(relations_doc_path)
    business_context_text = read_text(business_context_doc_path)

    databases = extract_databases(schema_text)
    tables = []
    views = []
    for database, db_text in databases.items():
        tables.extend(extract_tables(database, db_text))
        views.extend(extract_views(database, db_text))

    relationship_entries = extract_relation_entries(relations_text)
    relation_counts = relation_evidence_counts(relations_text)

    table_rows = build_table_rows(tables)
    view_rows = build_view_rows(views)
    relationship_rows = build_relationship_rows(relationship_entries)
    business_rule_rows = build_business_rule_rows(business_context_text, relation_counts)

    write_jsonl(output_dir / "nl2sql_schema_tables.jsonl", table_rows)
    write_jsonl(output_dir / "nl2sql_schema_views.jsonl", view_rows)
    write_jsonl(output_dir / "nl2sql_relationships.jsonl", relationship_rows)
    write_jsonl(output_dir / "nl2sql_business_rules.jsonl", business_rule_rows)
    (output_dir / "nl2sql_generated_manifest.json").write_text(
        json.dumps(build_manifest(output_dir, tables, views, relationship_entries), indent=2),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "tables": len(tables),
                "views": len(views),
                "relationships": len(relationship_entries),
                "output_dir": str(output_dir),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()