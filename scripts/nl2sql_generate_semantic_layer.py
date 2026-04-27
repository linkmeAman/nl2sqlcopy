#!/usr/bin/env python3
"""Generate a structured semantic knowledge layer for every base table.

Reads MySQL schema metadata live from the database, sends each table's
metadata to Gemini, and writes structured JSON output to a JSONL file.

Usage:
    ./.venv/bin/python scripts/nl2sql_generate_semantic_layer.py
    ./.venv/bin/python scripts/nl2sql_generate_semantic_layer.py --databases pf_TickleRight_9210
    ./.venv/bin/python scripts/nl2sql_generate_semantic_layer.py --tables member invoice contact
    ./.venv/bin/python scripts/nl2sql_generate_semantic_layer.py --resume
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pymysql
from dotenv import load_dotenv

# ── defaults ────────────────────────────────────────────────────────────────
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "generated"
OUTPUT_FILE = "nl2sql_semantic_layer.jsonl"
PROGRESS_FILE = "nl2sql_semantic_layer_progress.json"

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_API_BASE = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_PROVIDER = "groq"        # gemini | ollama | groq | openrouter
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"   # any model pulled via `ollama pull`
DEFAULT_GROQ_MODEL = "llama-3.1-70b-versatile"
DEFAULT_OPENROUTER_MODEL = "meta-llama/llama-3.1-8b-instruct:free"
DEFAULT_OLLAMA_URL = "http://localhost:11434"

MAX_SAMPLE_ROWS = 5
MAX_RETRIES = 3
RETRY_DELAY = 5          # seconds between retries
RATE_LIMIT_DELAY = 1.0   # seconds between successive LLM calls

SYSTEM_PROMPT = """\
You are a data architect and analytics engineer.

Your task is to convert raw database schema metadata into a structured semantic knowledge layer for a RAG-based SQL agent.

You will be given:
- Table name
- Columns (name + type)
- Primary keys
- Foreign keys
- (Optional) sample values / stats

Your job is to generate a structured JSON output that includes:

1. Table Summary
- Business-friendly description of what the table likely represents
- Estimated grain (one row per what?)
- Possible use cases

2. Column Grouping
Group columns into:
- identifiers
- timestamps
- status/enums
- numeric/measures
- foreign keys
- attributes

3. Key Columns Explanation
Explain important columns in plain English

4. Relationships
- Explain joins using foreign keys
- Suggest common join paths

5. Usage Patterns
- What kind of queries this table will be used for
- Example analytical questions

6. Data Caveats (IMPORTANT)
- Potential nulls
- status handling assumptions
- possible duplicates
- anything ambiguous

7. Retrieval Priority
Classify as:
- high (business-critical)
- medium (supporting)
- low (rarely needed / infra)

Output strictly in JSON format:

{
  "table_name": "",
  "description": "",
  "grain": "",
  "column_groups": {},
  "key_columns": {},
  "relationships": [],
  "use_cases": [],
  "caveats": [],
  "retrieval_priority": ""
}

Important rules:
- Do NOT hallucinate columns
- Infer meaning carefully from names
- If unsure, say "likely" or "appears to"
- Keep descriptions concise but useful
"""


# ── CLI ─────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate semantic layer via Gemini, Ollama, Groq, or OpenRouter.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
providers:
  gemini      Requires GEMINI_API_KEY in .env  (free tier at aistudio.google.com)
  ollama      Local model, no API key needed   (install: https://ollama.com)
  groq        Cloud, free tier                 (free key at console.groq.com)
  openrouter  Cloud, free models available     (free key at openrouter.ai)

examples:
  # Ollama with local Llama 3.1 (no API key needed)
  ./.venv/bin/python scripts/nl2sql_generate_semantic_layer.py --provider ollama

  # Groq free tier
  ./.venv/bin/python scripts/nl2sql_generate_semantic_layer.py --provider groq

  # Gemini free tier (get key at aistudio.google.com)
  ./.venv/bin/python scripts/nl2sql_generate_semantic_layer.py --provider gemini
"""
    )
    p.add_argument("--databases", nargs="*", help="Limit to these databases (default: all)")
    p.add_argument("--tables", nargs="*", help="Limit to these table names (default: all)")
    p.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        choices=["gemini", "ollama", "groq", "openrouter"],
        help="LLM provider to use (default: ollama)",
    )
    p.add_argument("--model", default=None, help="Override the default model for chosen provider")
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama base URL")
    p.add_argument("--output-dir", default=str(OUTPUT_DIR))
    p.add_argument("--resume", action="store_true", help="Skip tables already processed")
    p.add_argument("--dry-run", action="store_true", help="Extract metadata only, skip LLM")
    return p.parse_args()


# ── DB helpers ──────────────────────────────────────────────────────────────
def get_connection() -> pymysql.Connection:
    load_dotenv(ENV_PATH)
    return pymysql.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD", ""),
        port=int(os.getenv("DB_PORT", 3306)),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
    )


def list_databases(conn: pymysql.Connection, allowed: list[str] | None) -> list[str]:
    """Return database names that match the pf_* pattern."""
    with conn.cursor() as cur:
        cur.execute("SHOW DATABASES")
        dbs = [
            row["Database"]
            for row in cur.fetchall()
            if row["Database"].startswith("pf_")
        ]
    if allowed:
        dbs = [d for d in dbs if d in allowed]
    return sorted(dbs)


def list_base_tables(conn: pymysql.Connection, database: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_NAME",
            (database,),
        )
        return [row["TABLE_NAME"] for row in cur.fetchall()]


def get_columns(conn: pymysql.Connection, database: str, table: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION",
            (database, table),
        )
        return cur.fetchall()


def get_primary_keys(conn: pymysql.Connection, database: str, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND CONSTRAINT_NAME = 'PRIMARY' "
            "ORDER BY ORDINAL_POSITION",
            (database, table),
        )
        return [row["COLUMN_NAME"] for row in cur.fetchall()]


def get_foreign_keys(conn: pymysql.Connection, database: str, table: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_SCHEMA, "
            "REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "AND REFERENCED_TABLE_NAME IS NOT NULL "
            "ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION",
            (database, table),
        )
        return cur.fetchall()


def get_row_count(conn: pymysql.Connection, database: str, table: str) -> int | None:
    """Fast approximate row count from information_schema."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_ROWS FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (database, table),
        )
        row = cur.fetchone()
        return row["TABLE_ROWS"] if row else None


def get_sample_values(
    conn: pymysql.Connection, database: str, table: str, columns: list[str]
) -> dict[str, list]:
    """Get a few distinct sample values for each column (best-effort)."""
    samples: dict[str, list] = {}
    if not columns:
        return samples

    # pick at most 10 interesting columns to sample (avoid huge fetches)
    cols_to_sample = columns[:10]

    with conn.cursor() as cur:
        for col in cols_to_sample:
            try:
                safe_col = f"`{col}`"
                safe_table = f"`{database}`.`{table}`"
                cur.execute(
                    f"SELECT DISTINCT {safe_col} FROM {safe_table} "
                    f"WHERE {safe_col} IS NOT NULL LIMIT %s",
                    (MAX_SAMPLE_ROWS,),
                )
                rows = cur.fetchall()
                vals = [_serializable(r[col]) for r in rows]
                if vals:
                    samples[col] = vals
            except Exception:
                pass  # skip columns that error (e.g. BLOB)
    return samples


def _serializable(val):
    """Make a value JSON-serializable."""
    if val is None:
        return None
    if isinstance(val, (int, float, bool, str)):
        return val
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8", errors="replace")[:100]
        except Exception:
            return "<binary>"
    return str(val)[:200]


# ── LLM helpers ─────────────────────────────────────────────────────────────
def _http_post(url: str, body: dict, headers: dict, provider_label: str) -> dict:
    """Make a POST request with retry logic; return parsed JSON response."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode() if exc.fp else ""
            if exc.code == 429 or exc.code >= 500:
                wait = RETRY_DELAY * attempt
                print(f"  ⚠ {provider_label} HTTP {exc.code}, retry {attempt}/{MAX_RETRIES} in {wait}s …")
                time.sleep(wait)
                continue
            raise RuntimeError(f"{provider_label} API error {exc.code}: {err_body[:500]}") from exc
        except urllib.error.URLError as exc:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
                continue
            raise
    raise RuntimeError(f"{provider_label}: max retries exceeded")


def call_gemini(prompt: str, model: str, api_key: str) -> str:
    url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    result = _http_post(url, body, {"Content-Type": "application/json"}, "Gemini")
    return result["candidates"][0]["content"]["parts"][0]["text"]


def call_ollama(prompt: str, model: str, base_url: str) -> str:
    """Call Ollama using its OpenAI-compatible /v1/chat/completions endpoint."""
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    result = _http_post(url, body, {"Content-Type": "application/json"}, "Ollama")
    return result["choices"][0]["message"]["content"]


def call_openai_compatible(prompt: str, model: str, api_key: str, base_url: str, label: str) -> str:
    """Generic OpenAI-compatible chat completions call (Groq, OpenRouter, etc.)."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    result = _http_post(base_url, body, headers, label)
    return result["choices"][0]["message"]["content"]


def call_llm(prompt: str, provider: str, model: str, api_key: str, ollama_url: str) -> str:
    """Dispatch to the correct LLM backend."""
    if provider == "gemini":
        return call_gemini(prompt, model, api_key)
    elif provider == "ollama":
        return call_ollama(prompt, model, ollama_url)
    elif provider == "groq":
        return call_openai_compatible(prompt, model, api_key, GROQ_API_BASE, "Groq")
    elif provider == "openrouter":
        return call_openai_compatible(prompt, model, api_key, OPENROUTER_API_BASE, "OpenRouter")
    else:
        raise ValueError(f"Unknown provider: {provider}")


def extract_json(raw: str) -> dict:
    """Parse JSON from Gemini output, stripping markdown fences if present."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


# ── metadata builder ────────────────────────────────────────────────────────
def build_table_metadata(
    conn: pymysql.Connection, database: str, table: str
) -> dict:
    columns = get_columns(conn, database, table)
    pk = get_primary_keys(conn, database, table)
    fks = get_foreign_keys(conn, database, table)
    row_count = get_row_count(conn, database, table)
    col_names = [c["COLUMN_NAME"] for c in columns]
    samples = get_sample_values(conn, database, table, col_names)

    return {
        "database": database,
        "table": table,
        "full_name": f"{database}.{table}",
        "columns": [
            {
                "name": c["COLUMN_NAME"],
                "type": c["COLUMN_TYPE"],
                "nullable": c["IS_NULLABLE"],
                "key": c["COLUMN_KEY"],
                "default": _serializable(c["COLUMN_DEFAULT"]),
                "extra": c["EXTRA"],
                "comment": c["COLUMN_COMMENT"] or "",
            }
            for c in columns
        ],
        "primary_keys": pk,
        "foreign_keys": [
            {
                "constraint": fk["CONSTRAINT_NAME"],
                "column": fk["COLUMN_NAME"],
                "ref_schema": fk["REFERENCED_TABLE_SCHEMA"],
                "ref_table": fk["REFERENCED_TABLE_NAME"],
                "ref_column": fk["REFERENCED_COLUMN_NAME"],
            }
            for fk in fks
        ],
        "approx_row_count": row_count,
        "sample_values": samples,
    }


def metadata_to_prompt(meta: dict) -> str:
    """Build a user-prompt from extracted metadata."""
    lines = [
        f"Table: {meta['full_name']}",
        f"Approximate row count: {meta['approx_row_count']}",
        "",
        "Columns:",
    ]
    for c in meta["columns"]:
        parts = [f"  - {c['name']} ({c['type']})"]
        if c["key"]:
            parts.append(f"[KEY={c['key']}]")
        if c["nullable"] == "YES":
            parts.append("[NULLABLE]")
        if c["extra"]:
            parts.append(f"[{c['extra']}]")
        if c["comment"]:
            parts.append(f"-- {c['comment']}")
        lines.append(" ".join(parts))

    if meta["primary_keys"]:
        lines.append(f"\nPrimary Key: {', '.join(meta['primary_keys'])}")

    if meta["foreign_keys"]:
        lines.append("\nForeign Keys:")
        for fk in meta["foreign_keys"]:
            lines.append(
                f"  - {fk['column']} -> {fk['ref_schema']}.{fk['ref_table']}.{fk['ref_column']} "
                f"(constraint: {fk['constraint']})"
            )
    else:
        lines.append("\nForeign Keys: none declared in schema")

    if meta["sample_values"]:
        lines.append("\nSample distinct values:")
        for col, vals in meta["sample_values"].items():
            lines.append(f"  {col}: {vals}")

    return "\n".join(lines)


# ── progress tracking ──────────────────────────────────────────────────────
def load_progress(path: Path) -> set[str]:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("completed", []))
    return set()


def save_progress(path: Path, completed: set[str]) -> None:
    path.write_text(
        json.dumps({"completed": sorted(completed)}, indent=2),
        encoding="utf-8",
    )


# ── main ────────────────────────────────────────────────────────────────────
# ── env key names per provider ──────────────────────────────────────────────
_PROVIDER_ENV_KEY = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "ollama": None,  # no key needed
}

_PROVIDER_DEFAULT_MODEL = {
    "gemini": DEFAULT_GEMINI_MODEL,
    "ollama": DEFAULT_OLLAMA_MODEL,
    "groq": DEFAULT_GROQ_MODEL,
    "openrouter": DEFAULT_OPENROUTER_MODEL,
}


def main() -> None:
    args = parse_args()
    load_dotenv(ENV_PATH)

    provider = args.provider
    model = args.model or _PROVIDER_DEFAULT_MODEL[provider]
    env_key_name = _PROVIDER_ENV_KEY[provider]
    api_key = os.getenv(env_key_name, "").strip() if env_key_name else ""

    if env_key_name and not api_key and not args.dry_run:
        print(f"ERROR: {env_key_name} not set in .env (required for provider '{provider}')")
        if provider == "gemini":
            print("  Get a free key at: https://aistudio.google.com")
        elif provider == "groq":
            print("  Get a free key at: https://console.groq.com")
        elif provider == "openrouter":
            print("  Get a free key at: https://openrouter.ai")
        sys.exit(1)

    if provider == "ollama" and not args.dry_run:
        # quick connectivity check
        try:
            urllib.request.urlopen(f"{args.ollama_url}/api/tags", timeout=5)
        except Exception:
            print(f"ERROR: Cannot reach Ollama at {args.ollama_url}")
            print("  Install guide: https://ollama.com")
            print(f"  Then pull a model: ollama pull {model}")
            sys.exit(1)

    print(f"Provider : {provider}")
    print(f"Model    : {model}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / OUTPUT_FILE
    progress_path = output_dir / PROGRESS_FILE

    completed = load_progress(progress_path) if args.resume else set()

    conn = get_connection()
    databases = list_databases(conn, args.databases)
    print(f"Databases: {databases}")

    # collect all tables
    work: list[tuple[str, str]] = []
    for db in databases:
        tables = list_base_tables(conn, db)
        if args.tables:
            tables = [t for t in tables if t in args.tables]
        for t in tables:
            full = f"{db}.{t}"
            if full not in completed:
                work.append((db, t))

    total = len(work) + len(completed)
    print(f"Tables to process: {len(work)} (already done: {len(completed)}, total: {total})")

    if not work:
        print("Nothing to do.")
        conn.close()
        return

    # open output in append mode so resume works
    mode = "a" if args.resume and output_path.exists() else "w"
    fh = open(output_path, mode, encoding="utf-8")

    success = 0
    errors = []
    start_done = len(completed)

    for idx, (db, table) in enumerate(work, start=1):
        full_name = f"{db}.{table}"
        pct = (start_done + idx) / total * 100
        print(f"\n[{start_done+idx}/{total}] ({pct:.0f}%) {full_name}")

        try:
            meta = build_table_metadata(conn, db, table)
            print(f"  columns={len(meta['columns'])}  fks={len(meta['foreign_keys'])}  rows≈{meta['approx_row_count']}")

            if args.dry_run:
                prompt = metadata_to_prompt(meta)
                print(f"  [dry-run] prompt length: {len(prompt)} chars")
                row = {"table_name": full_name, "metadata": meta, "semantic": None}
            else:
                prompt = metadata_to_prompt(meta)
                raw = call_llm(prompt, provider, model, api_key, args.ollama_url)
                semantic = extract_json(raw)
                row = {
                    "table_name": full_name,
                    "metadata": meta,
                    "semantic": semantic,
                }
                time.sleep(RATE_LIMIT_DELAY)

            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            completed.add(full_name)
            save_progress(progress_path, completed)
            success += 1

        except Exception as exc:
            msg = f"{full_name}: {exc}"
            print(f"  ✗ {msg}")
            errors.append(msg)

    fh.close()
    conn.close()

    print(f"\n{'='*60}")
    print(f"Done. success={success}  errors={len(errors)}")
    print(f"Output: {output_path}")
    if errors:
        print("\nFailed tables:")
        for e in errors:
            print(f"  - {e}")


if __name__ == "__main__":
    main()
