import argparse
import difflib
import re
import sys
from pathlib import Path


DEFAULT_SCHEMA_EXPORT = "docs/mysql_schema_export.txt"
KEYWORDS = {
    "select", "from", "where", "join", "left", "right", "inner", "outer", "full",
    "on", "and", "or", "not", "as", "order", "by", "group", "having", "limit",
    "offset", "asc", "desc", "distinct", "case", "when", "then", "else", "end",
    "is", "null", "like", "in", "between", "exists", "union", "all", "if", "ifnull",
    "convert", "cast", "date_format", "time_format", "curdate", "now", "last_day",
    "interval", "current_date", "current_timestamp", "date", "true", "false",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate generated SQL against the local schema export.")
    parser.add_argument("--sql", help="SQL text to validate")
    parser.add_argument("--sql-file", help="Path to a file containing SQL to validate")
    parser.add_argument("--schema-export", default=DEFAULT_SCHEMA_EXPORT)
    return parser.parse_args()


def read_sql(args: argparse.Namespace) -> str:
    if args.sql:
        return args.sql
    if args.sql_file:
        return Path(args.sql_file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Provide --sql, --sql-file, or pipe SQL on stdin.")


def strip_comments_and_literals(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"#[^\n]*", " ", sql)
    sql = re.sub(r"'(?:''|[^'])*'", "''", sql)
    sql = re.sub(r'"(?:\\"|[^"])*"', '""', sql)
    return sql


def normalize_name(name: str) -> str:
    return name.replace("`", "").strip()


def extract_object_refs(sql: str) -> list[dict]:
    pattern = re.compile(
        r"\b(?:FROM|JOIN)\s+((?:`[^`]+`|\w+)(?:\.(?:`[^`]+`|\w+)){0,2})(?:\s+(?:AS\s+)?(`[^`]+`|\w+))?",
        re.IGNORECASE,
    )
    refs = []
    for match in pattern.finditer(sql):
        raw_object = normalize_name(match.group(1))
        if raw_object.startswith("("):
            continue
        parts = raw_object.split(".")
        simple_name = parts[-1]
        alias = normalize_name(match.group(2)) if match.group(2) else simple_name
        if alias.lower() in KEYWORDS:
            alias = simple_name
        refs.append(
            {
                "raw": raw_object,
                "name": simple_name,
                "alias": alias,
            }
        )
    return refs


def load_schema_export(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def unique_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    ordered = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def extract_view_columns(schema_text: str, name: str) -> list[str]:
    pattern = re.compile(
        rf"VIEW:\s+[^\n]*\.{re.escape(name)}\s*\n-+\n(CREATE .*?;)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(schema_text)
    if not match:
        return []
    statement = match.group(1)
    aliases = re.findall(r"\sAS\s+`([^`]+)`", statement, flags=re.IGNORECASE)
    return unique_preserve_order(aliases)


def extract_table_columns(schema_text: str, name: str) -> list[str]:
    pattern = re.compile(
        rf"CREATE TABLE `{re.escape(name)}`\s*\((.*?)\)\s*ENGINE=",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(schema_text)
    if not match:
        return []
    body = match.group(1)
    columns = re.findall(r"^\s*`([^`]+)`\s", body, flags=re.MULTILINE)
    return unique_preserve_order(columns)


def load_columns_for_object(schema_text: str, name: str) -> list[str]:
    columns = extract_view_columns(schema_text, name)
    if columns:
        return columns
    return extract_table_columns(schema_text, name)


def extract_qualified_refs(sql: str, alias_map: dict[str, str]) -> list[tuple[str, str]]:
    refs = []
    for lhs, rhs in re.findall(r"\b([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\b", sql):
        if lhs in alias_map:
            refs.append((lhs, rhs))
    return refs


def extract_bare_tokens(sql: str, alias_map: dict[str, str], object_names: set[str]) -> list[str]:
    sql = sql.replace("`", "")
    sql = re.sub(r"\b[A-Za-z_][\w]*\.[A-Za-z_][\w]*\b", " ", sql)
    tokens = []
    matches = list(re.finditer(r"\b[A-Za-z_][\w]*\b", sql))
    lowered = [match.group(0).lower() for match in matches]
    for index, match in enumerate(matches):
        token = match.group(0)
        lower = lowered[index]
        if lower in KEYWORDS:
            continue
        if token in alias_map or token in object_names:
            continue
        if index > 0 and lowered[index - 1] in {"as", "from", "join", "update", "into"}:
            continue
        next_char_index = match.end()
        if next_char_index < len(sql) and sql[next_char_index: next_char_index + 1] == "(":
            continue
        tokens.append(token)
    return unique_preserve_order(tokens)


def close_matches(token: str, candidates: list[str]) -> list[str]:
    return difflib.get_close_matches(token, candidates, n=3, cutoff=0.5)


def main() -> None:
    args = parse_args()
    sql = read_sql(args)
    schema_export_path = Path(args.schema_export)
    schema_text = load_schema_export(schema_export_path)
    cleaned_sql = strip_comments_and_literals(sql)

    refs = extract_object_refs(cleaned_sql)
    if not refs:
        raise SystemExit("Could not find any FROM or JOIN objects in the SQL.")

    alias_map = {ref["alias"]: ref["name"] for ref in refs}
    object_names = {ref["name"] for ref in refs}
    column_map = {name: load_columns_for_object(schema_text, name) for name in object_names}
    unknown_objects = sorted(name for name, columns in column_map.items() if not columns)

    invalid = []
    qualified_refs = extract_qualified_refs(cleaned_sql, alias_map)
    for alias, column in qualified_refs:
        obj = alias_map[alias]
        allowed = column_map.get(obj, [])
        if allowed and column not in allowed:
            invalid.append(
                {
                    "type": "qualified",
                    "object": obj,
                    "alias": alias,
                    "column": column,
                    "suggestions": close_matches(column, allowed),
                }
            )

    if not unknown_objects:
        bare_tokens = extract_bare_tokens(cleaned_sql, alias_map, object_names)
        known_columns = {name: set(columns) for name, columns in column_map.items()}
        union_columns = sorted({column for columns in column_map.values() for column in columns})
        for token in bare_tokens:
            owners = [name for name, columns in known_columns.items() if token in columns]
            if not owners:
                invalid.append(
                    {
                        "type": "bare",
                        "object": "",
                        "alias": "",
                        "column": token,
                        "suggestions": close_matches(token, union_columns),
                    }
                )

    print("Referenced objects:")
    for ref in refs:
        columns = column_map.get(ref["name"], [])
        detail = f"{ref['raw']} as {ref['alias']}"
        if columns:
            detail += f" ({len(columns)} known columns)"
        else:
            detail += " (no catalog found in schema export)"
        print(f"- {detail}")

    if unknown_objects:
        print("\nValidation note:")
        print(
            "- Some referenced objects were not found in the schema export, so bare-column validation was skipped "
            f"for those cases: {', '.join(unknown_objects)}"
        )

    if invalid:
        print("\nInvalid or unknown columns:")
        for item in invalid:
            prefix = f"{item['alias']}." if item["alias"] else ""
            line = f"- {prefix}{item['column']}"
            if item["object"]:
                line += f" on {item['object']}"
            if item["suggestions"]:
                line += f" | suggestions: {', '.join(item['suggestions'])}"
            print(line)
        raise SystemExit(1)

    print("\nSQL validation passed.")


if __name__ == "__main__":
    main()
