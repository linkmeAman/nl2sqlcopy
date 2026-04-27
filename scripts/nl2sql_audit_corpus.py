import argparse
import json
from pathlib import Path


DEFAULT_GENERATED_DIR = "docs/generated"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit generated NL2SQL corpus coverage against the build manifest."
    )
    parser.add_argument("--generated-dir", default=DEFAULT_GENERATED_DIR)
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def object_identity(row: dict) -> str:
    full_name = row.get("full_object_name")
    if full_name:
        return full_name
    database = row.get("database", "")
    object_name = row.get("object_name", "")
    return f"{database}.{object_name}".strip(".")


def main() -> None:
    args = parse_args()
    generated_dir = Path(args.generated_dir)
    manifest = load_json(generated_dir / "nl2sql_generated_manifest.json")

    table_rows = load_jsonl(generated_dir / manifest["files"]["tables"])
    view_rows = load_jsonl(generated_dir / manifest["files"]["views"])
    relationship_rows = load_jsonl(generated_dir / manifest["files"]["relationships"])
    business_rule_rows = load_jsonl(generated_dir / manifest["files"]["business_rules"])

    unique_tables = {object_identity(row) for row in table_rows if object_identity(row)}
    unique_views = {object_identity(row) for row in view_rows if object_identity(row)}
    relation_confidences = {}
    for row in relationship_rows:
        confidence = row.get("confidence", "unknown")
        relation_confidences[confidence] = relation_confidences.get(confidence, 0) + 1

    report = {
        "manifest_expected": {
            "tables": manifest.get("table_count", 0),
            "views": manifest.get("view_count", 0),
            "relationships": manifest.get("relationship_count", 0),
        },
        "observed": {
            "table_rows": len(table_rows),
            "view_rows": len(view_rows),
            "relationship_rows": len(relationship_rows),
            "business_rule_rows": len(business_rule_rows),
            "unique_tables": len(unique_tables),
            "unique_views": len(unique_views),
        },
        "coverage": {
            "tables_complete": len(unique_tables) == manifest.get("table_count", 0),
            "views_complete": len(unique_views) == manifest.get("view_count", 0),
            "relationships_complete": len(relationship_rows) == manifest.get("relationship_count", 0),
        },
        "relationship_confidence_counts": relation_confidences,
        "databases": manifest.get("databases", {}),
    }

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()