# NL2SQL — Natural Language to SQL Context & Generation System

A RAG-based pipeline that converts natural language questions into MySQL queries for the **WebPortal** (TickleRight) platform. It works by embedding database schema, relationships, and business context into a **Qdrant** vector store, then retrieving relevant context at query time and sending it to **Google Gemini** for SQL generation.

---

## Architecture Overview

```
 ┌──────────────────────────────────────────────────────────────────┐
 │  Sources                                                        │
 │  ├── docs/mysql_schema_export.txt   (692 tables · 373 views)    │
 │  ├── docs/database-table-relations.md   (62 documented joins)   │
 │  ├── docs/vector-db-schema-business-context.md                  │
 │  ├── docs/nl2sql-corpus.jsonl       (hand-curated overrides)    │
 │  └── docs/nl2sql-columns.jsonl      (column-level docs)         │
 └──────────────────────────┬───────────────────────────────────────┘
                            │
               ┌────────────▼────────────┐
               │  nl2sql_build_corpus.py │  ← Step 1: Parse & chunk
               └────────────┬────────────┘
                            │
               docs/generated/*.jsonl + manifest
                            │
               ┌────────────▼────────────┐
               │ nl2sql_ingest_qdrant.py │  ← Step 2: Embed & index
               └────────────┬────────────┘
                            │
                   Qdrant vector DB
                   (webportal_nl2sql_context)
                            │
          ┌─────────────────┼─────────────────┐
          │                 │                 │
  ┌───────▼───────┐ ┌──────▼──────┐ ┌────────▼────────┐
  │ query_qdrant  │ │  generate   │ │  validate_sql   │
  │ (debug/test)  │ │  _gemini    │ │  (safety gate)  │
  └───────────────┘ │ (NL → SQL)  │ └─────────────────┘
                    └─────────────┘
```

---

## Databases Covered

| Database              | Tables | Views | Role                                  |
|-----------------------|--------|-------|---------------------------------------|
| `pf_TickleRight_9210` | 528    | 366   | Core tenant data (CRM, billing, ops)  |
| `pf_central`          | 131    | 7     | Shared platform (users, permissions)  |
| `pf_messenger`        | 33     | —     | Messaging system                      |

---

## Prerequisites

- **Python 3.10+** (using `.venv`)
- **Qdrant** running on `http://localhost:6333`
- **MySQL** accessible via credentials in `.env`
- **Google Gemini API key** for SQL generation

### Python Dependencies

```bash
python -m venv .venv
./.venv/bin/pip install fastembed qdrant-client pymysql python-dotenv cryptography
```

### Environment Configuration (`.env`)

```env
GEMINI_API_KEY=your-gemini-api-key-here
DB_HOST=localhost
DB_USER=readonly_user
DB_PASSWORD=your-password
DB_PORT=3306
```

---

## Pipeline — Step by Step

### Step 1: Build the Corpus

Parses the raw MySQL schema export, relationship documentation, and business context narrative into structured JSONL files ready for embedding.

```bash
./.venv/bin/python scripts/nl2sql_build_corpus.py
```

**Inputs:**
- `docs/mysql_schema_export.txt` — Full DDL export (19k lines)
- `docs/database-table-relations.md` — Manually documented joins with confidence levels
- `docs/vector-db-schema-business-context.md` — Business domain narrative

**Outputs** (in `docs/generated/`):

| File                              | Rows | Content                                  |
|-----------------------------------|------|------------------------------------------|
| `nl2sql_schema_tables.jsonl`      | ~695 | One chunk per base table (max 80 cols)   |
| `nl2sql_schema_views.jsonl`       | ~392 | One chunk per view + dependencies        |
| `nl2sql_relationships.jsonl`      | 62   | Documented joins with confidence levels  |
| `nl2sql_business_rules.jsonl`     | ~21  | Business context paragraphs              |
| `nl2sql_generated_manifest.json`  | 1    | Metadata manifest for downstream tools   |

---

### Step 2: Ingest into Qdrant

Embeds all corpus JSONL files using **FastEmbed** (`all-MiniLM-L6-v2`) and upserts them into the Qdrant collection.

```bash
./.venv/bin/python scripts/nl2sql_ingest_qdrant.py \
  --manifest docs/generated/nl2sql_generated_manifest.json \
  --corpus docs/nl2sql-corpus.jsonl \
  --corpus docs/nl2sql-columns.jsonl
```

| Option         | Default                              | Description                          |
|----------------|--------------------------------------|--------------------------------------|
| `--qdrant-url` | `http://localhost:6333`              | Qdrant endpoint                      |
| `--collection` | `webportal_nl2sql_context`           | Target collection name               |
| `--model`      | `sentence-transformers/all-MiniLM-L6-v2` | Embedding model               |
| `--manifest`   | —                                    | Manifest JSON (auto-expands files)   |
| `--corpus`     | —                                    | Additional JSONL files (repeatable)  |
| `--batch-size` | 32                                   | Embedding batch size                 |

---

### Step 3: Query — Generate SQL from Natural Language

The main entry point. Retrieves relevant schema context from Qdrant, then sends the enriched prompt to Gemini.

```bash
./.venv/bin/python scripts/nl2sql_generate_gemini.py \
  "show active packages expiring this month by branch" \
  --show-context
```

**How it works internally:**
1. Embeds the user question with FastEmbed
2. Retrieves top candidates from Qdrant (limit × candidate_multiplier)
3. Filters by minimum semantic score (0.35)
4. Forces inclusion of column-catalog chunks and required doc IDs
5. Deduplicates context
6. Builds system prompt with rules (prefer views, don't invent columns, use `park=0`, etc.)
7. Sends to Gemini → returns SQL + assumptions

| Option                       | Default              | Description                              |
|------------------------------|----------------------|------------------------------------------|
| `--gemini-model`             | `gemini-flash-latest`| Gemini model variant                     |
| `--limit`                    | 8                    | Max context chunks                       |
| `--min-score`                | 0.35                 | Semantic similarity threshold            |
| `--candidate-multiplier`     | 4                    | Over-fetch factor for pre-filtering      |
| `--require-columns-context`  | true                 | Always include a column catalog chunk    |
| `--show-context`             | false                | Print retrieved context before SQL       |

**Key generation rules enforced by the system prompt:**
- Prefer enriched views over raw tables when they match the business meaning
- Never invent foreign keys or column names
- Qualify schema names when mixing databases
- Default to `park = 0` for active operational data
- Treat `request.table_name + request.row_id` as polymorphic (not a fixed FK)

---

### Step 4: Validate Generated SQL

Run generated SQL through schema validation before executing it against the database.

```bash
# From a file
./.venv/bin/python scripts/nl2sql_validate_sql.py --sql-file query.sql

# Inline
./.venv/bin/python scripts/nl2sql_validate_sql.py --sql "SELECT branch, COUNT(*) FROM member GROUP BY branch"

# From stdin (pipe)
echo "SELECT * FROM member" | ./.venv/bin/python scripts/nl2sql_validate_sql.py
```

**What it checks:**
- All `FROM`/`JOIN` references exist in the schema
- All qualified column references (`alias.column`) exist on their table/view
- All bare column tokens exist on at least one referenced object
- Suggests close matches (fuzzy) for unrecognized names

---

## Utility Scripts

### Debug Retrieval — `nl2sql_query_qdrant.py`

Inspect what context chunks Qdrant returns for a given question (without calling Gemini).

```bash
./.venv/bin/python scripts/nl2sql_query_qdrant.py \
  "total revenue by branch last quarter" \
  --limit 10
```

### Audit Corpus — `nl2sql_audit_corpus.py`

Verify the generated corpus is complete against the manifest.

```bash
./.venv/bin/python scripts/nl2sql_audit_corpus.py
```

Returns a JSON report with coverage flags (`tables_complete`, `views_complete`, `relationships_complete`), row counts, and per-database breakdowns.

### Generate Semantic Layer — `nl2sql_generate_semantic_layer.py`

Optional enrichment step: connects directly to MySQL, extracts live metadata (columns, types, PKs, FKs, row counts, sample values), and sends each table to Gemini for semantic annotation.

```bash
# All tables (692 tables — takes time, supports resume)
./.venv/bin/python scripts/nl2sql_generate_semantic_layer.py --resume

# Specific tables
./.venv/bin/python scripts/nl2sql_generate_semantic_layer.py --tables member invoice contact

# Dry run — extract metadata only, no Gemini calls
./.venv/bin/python scripts/nl2sql_generate_semantic_layer.py --dry-run
```

**Output:** `docs/generated/nl2sql_semantic_layer.jsonl` — one JSON row per table:
```json
{
  "table_name": "pf_TickleRight_9210.member",
  "metadata": { "columns": [...], "primary_keys": [...], "foreign_keys": [...], "approx_row_count": 17474, "sample_values": {...} },
  "semantic": {
    "table_name": "member",
    "description": "Core membership record...",
    "grain": "one row per member enrollment",
    "column_groups": { "identifiers": [...], "timestamps": [...], ... },
    "key_columns": { "contact_id": "Links to the contact record...", ... },
    "relationships": [...],
    "use_cases": ["Active member counts by branch", ...],
    "caveats": ["status values need mapping...", ...],
    "retrieval_priority": "high"
  }
}
```

---

## Business Domain Context

This system serves the **TickleRight** platform — a franchise management system for children's education (Right Brain Development). Key business domains:

| Domain                | Core Tables                                    | Key Views                         |
|-----------------------|------------------------------------------------|-----------------------------------|
| **CRM / Enrollment**  | `contact`, `inquiry`, `followup`, `member`     | `active_inactive_member_view`     |
| **Scheduling**         | `batch`, `session`, `attendance`               | `batch_view`, `session_batch_view`|
| **Billing**            | `invoice`, `invoice_item`, `payment`           | `invoice_payment_view`            |
| **Curriculum**         | `service`, `module`, `topic`                   | —                                 |
| **Franchise Ops**      | `branch`, `venue`, `employee`                  | `OM_fran_*`, `OI_fran_*`         |

**Important modeling notes:**
- Most relationships are **application-inferred** or **view-defined**, not enforced by MySQL foreign keys
- `followup.master_id` is **polymorphic** — can point to inquiries, payments, or invoice items
- `request.table_name + request.row_id` is also polymorphic
- The `park` column (0/1) across most tables acts as a **soft-delete** flag — `park=0` means active
- Many reporting queries should prefer **views** which already encode business logic

---

## Update Cycle

When the MySQL schema changes:

```
1.  Re-export schema  →  docs/mysql_schema_export.txt
2.  Rebuild corpus    →  ./.venv/bin/python scripts/nl2sql_build_corpus.py
3.  Re-ingest         →  ./.venv/bin/python scripts/nl2sql_ingest_qdrant.py --manifest ...
4.  (Optional)        →  ./.venv/bin/python scripts/nl2sql_generate_semantic_layer.py --resume
5.  Audit             →  ./.venv/bin/python scripts/nl2sql_audit_corpus.py
```

---

## Project Structure

```
nl2sql/
├── .env                              # DB + Gemini credentials
├── ingest_context.py                 # Legacy placeholder (use nl2sql_ingest_qdrant.py)
├── scripts/
│   ├── nl2sql_build_corpus.py        # Step 1: Schema → JSONL corpus
│   ├── nl2sql_ingest_qdrant.py       # Step 2: JSONL → Qdrant vectors
│   ├── nl2sql_generate_gemini.py     # Step 3: NL question → SQL via RAG + Gemini
│   ├── nl2sql_validate_sql.py        # Step 4: Validate SQL against schema
│   ├── nl2sql_query_qdrant.py        # Utility: Debug vector retrieval
│   ├── nl2sql_audit_corpus.py        # Utility: Verify corpus completeness
│   └── nl2sql_generate_semantic_layer.py  # Optional: LLM-powered table docs
├── docs/
│   ├── mysql_schema_export.txt       # Full MySQL DDL (19k lines, 3 databases)
│   ├── database-table-relations.md   # 62 documented relationships
│   ├── vector-db-schema-business-context.md  # Business domain narrative
│   ├── nl2sql-corpus.jsonl           # Hand-curated context overrides
│   ├── nl2sql-columns.jsonl          # Column-level documentation
│   ├── nl2sql-ubuntu-server-guide.md # Deployment guide
│   └── generated/                    # Auto-generated corpus files
│       ├── nl2sql_schema_tables.jsonl
│       ├── nl2sql_schema_views.jsonl
│       ├── nl2sql_relationships.jsonl
│       ├── nl2sql_business_rules.jsonl
│       ├── nl2sql_generated_manifest.json
│       └── nl2sql_semantic_layer.jsonl      # (from semantic layer script)
└── qdrant_storage/                   # Local Qdrant data (collection: webportal_nl2sql_context)
```
