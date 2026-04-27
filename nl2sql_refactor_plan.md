# NL2SQL Schema Registry Refactor Plan

**Date Created:** April 23, 2026  
**Status:** ⚠️ SUPERSEDED — Implementation diverged; see [implementation_complete.md](implementation_complete.md) and [README.md](README.md) for actual architecture.  
**Original Scope:** All 6 fixes + per-group versioning + import-time validation

> **Note (April 24, 2026):** The plan below describes a `schema_registry.py`-based approach.  
> What was actually built uses `nl2sql_service/schema_loader.py` reading from `rag_schema/` JSON files.  
> This document is preserved for historical reference only.

---

## Problem Statement

The NL2SQL RAG service has six critical issues preventing production-grade schema management:

1. **Hardcoded Group Definitions** — Group definitions embedded in ingest scripts (`nl2sql_ingest_groups.py`); changes require multi-file edits
2. **Silent Chunk Staleness** — `ON CONFLICT DO NOTHING` prevents schema updates from refreshing embeddings; users don't know chunks are stale
3. **Contact Duplication** — Full contact schema embedded in both `sales_pipeline` and `billing` groups; wastes vector budget and causes retrieval collisions
4. **Silent Token Truncation** — Groups exceeding 512 tokens silently truncated without developer awareness; data loss goes undetected
5. **IVFFlat Index Brittleness** — IVFFlat requires manual `lists` tuning (~√row_count) and periodic `VACUUM ANALYZE`; recall degrades unpredictably
6. **Multi-File Registration** — Adding a new schema group requires editing `chunker.py`, `ingest.py`, `models.py`, and scripts; high friction and error-prone

## Solution Overview

- **Single Source of Truth**: Create `nl2sql_service/schema_registry.py` with all table and group definitions; all other code imports from it
- **Per-Group Versioning**: Each group has independent schema_version (e.g., `sales_pipeline:1.0`, `billing:1.0`); enables surgical updates
- **Version-Aware Upsert**: `ON CONFLICT DO UPDATE` with `WHERE schema_version !=` clause; stale chunks auto-refresh
- **Contact Deduplication**: `sales_pipeline` and `billing` link to `contact` (FK reference), not embed full schema; only `contact_root` embeds contact
- **Token Guard**: `chunk_schema_group()` estimates tokens; raises `ValueError` if > 400; prevents silent truncation
- **HNSW Index**: Replace IVFFlat with HNSW (m=16, ef_construction=64); no lists tuning, consistent recall
- **Import-Time Validation**: `validate_registry()` runs at module load; catches config errors immediately; fail-fast pattern
- **Single-File Pattern**: New tables/groups require editing ONLY `schema_registry.py`

---

## Implementation Sequence

**Critical**: Apply fixes in this exact order—dependencies cascade.

| Step | Fix | File(s) | Approx Time |
|------|-----|---------|-------------|
| 1 | Create schema_registry.py | `nl2sql_service/schema_registry.py` | 15 min |
| 2 | Add per-group versions & validate_registry | `nl2sql_service/schema_registry.py` (update) | 10 min |
| 3 | Apply contact dedup in chunker | `nl2sql_service/chunker.py` | 10 min |
| 4 | Add token guard in chunker | `nl2sql_service/chunker.py` | 5 min |
| 5 | Implement ingest_schema_groups | `nl2sql_service/ingest.py` | 15 min |
| 6 | Add ensure_hnsw_index | `nl2sql_service/ingest.py` (update) | 10 min |
| 7 | Add GroupQueryResponse model | `nl2sql_service/models.py` | 5 min |
| 8 | Implement retrieve_groups | `nl2sql_service/retrieve.py` | 10 min |
| 9 | Add new endpoints | `nl2sql_service/main.py` | 10 min |
| 10 | Update db.py for HNSW | `nl2sql_service/db.py` | 5 min |
| 11 | Refactor ingest script | `scripts/nl2sql_ingest_groups.py` | 5 min |
| 12 | Update README | `README.md` | 5 min |
| 13 | Verify & test | Integration tests | 15 min |
| | **Total** | | **~120 min (~2 hours)** |

---

## Fix 1: Create schema_registry.py (Single Source of Truth)

### File
**Create:** `nl2sql_service/schema_registry.py` (NEW)

### Purpose
Central registry defining:
- All 8 database tables with columns, descriptions, relationships, schema versions
- All 4 schema groups with tables, link notes, schema versions
- Helper functions for registry lookups
- Import-time validation to catch config errors immediately

### Implementation

```python
# nl2sql_service/schema_registry.py
"""
Single source of truth for all table and schema-group definitions.

This module is imported by chunker.py, ingest.py, and retrieve.py.
Adding a new table or group requires ONLY editing this file.

CHANGELOG:
- tables:1.0 (2026-04-23): Initial schema with 8 core tables (contact, inquiry, followup, member, invoice, invoice_item, payment, employee)
"""

# Global schema version for all table definitions (separate from per-group versions below)
TABLE_SCHEMA_VERSION = "tables:1.0"

SCHEMA = {
    "contact": {
        "columns": [
            "contact_id",
            "first_name",
            "last_name",
            "email",
            "phone",
            "company",
            "job_title",
            "address",
            "city",
            "state",
            "zip_code",
            "country",
            "last_activity",
            "date_created",
            "notes",
        ],
        "description": "Core contact entity; single source of truth for people and company info. Links inquiry, followup, member (many-to-one FK).",
        "related_to": [],
        "schema_version": TABLE_SCHEMA_VERSION,
    },
    "inquiry": {
        "columns": [
            "inquiry_id",
            "contact_id",
            "inquiry_type",
            "subject",
            "description",
            "status",
            "assigned_to",
            "created_at",
            "updated_at",
        ],
        "description": "Sales inquiry from contact; one-to-many with contact. Typically short-lived (won/lost). Links to employee (assigned_to).",
        "related_to": ["contact", "employee"],
        "schema_version": TABLE_SCHEMA_VERSION,
    },
    "followup": {
        "columns": [
            "followup_id",
            "inquiry_id",
            "contact_id",
            "followup_type",
            "notes",
            "scheduled_date",
            "completed_date",
            "created_at",
        ],
        "description": "Follow-up action on inquiry; one-to-many with inquiry. Tracks outreach cadence and engagement history.",
        "related_to": ["inquiry", "contact"],
        "schema_version": TABLE_SCHEMA_VERSION,
    },
    "member": {
        "columns": [
            "member_id",
            "contact_id",
            "membership_type",
            "start_date",
            "end_date",
            "status",
            "renewal_date",
            "notes",
        ],
        "description": "Membership or subscription record for contact; one-to-many with contact. Tracks lifecycle (active, expired, suspended).",
        "related_to": ["contact"],
        "schema_version": TABLE_SCHEMA_VERSION,
    },
    "invoice": {
        "columns": [
            "invoice_id",
            "contact_id",
            "invoice_number",
            "invoice_date",
            "due_date",
            "total_amount",
            "status",
            "notes",
            "created_at",
        ],
        "description": "Billing invoice issued to contact; one-to-many with contact. Links invoice_item (one-to-many) and payment (one-to-many).",
        "related_to": ["contact", "invoice_item", "payment"],
        "schema_version": TABLE_SCHEMA_VERSION,
    },
    "invoice_item": {
        "columns": [
            "invoice_item_id",
            "invoice_id",
            "description",
            "quantity",
            "unit_price",
            "line_amount",
            "tax_amount",
        ],
        "description": "Line item on invoice; one-to-many with invoice. Represents individual products/services sold.",
        "related_to": ["invoice"],
        "schema_version": TABLE_SCHEMA_VERSION,
    },
    "payment": {
        "columns": [
            "payment_id",
            "invoice_id",
            "contact_id",
            "payment_method",
            "amount",
            "payment_date",
            "reference_number",
            "status",
            "notes",
        ],
        "description": "Payment record for invoice; one-to-many with invoice, many-to-one with contact. Tracks payment history and reconciliation.",
        "related_to": ["invoice", "contact"],
        "schema_version": TABLE_SCHEMA_VERSION,
    },
    "employee": {
        "columns": [
            "employee_id",
            "first_name",
            "last_name",
            "email",
            "phone",
            "job_title",
            "department",
            "manager_id",
            "hire_date",
            "status",
            "notes",
        ],
        "description": "Internal employee record; assigned to inquiries and support tickets. Supports self-referencing manager hierarchy.",
        "related_to": ["inquiry"],
        "schema_version": TABLE_SCHEMA_VERSION,
    },
}

# CHANGELOG for schema groups:
# - sales_pipeline:1.0 (2026-04-23): inquiry, followup, employee; links to contact (dedup)
# - billing:1.0 (2026-04-23): invoice, invoice_item, payment; links to contact (dedup)
# - contact_root:1.0 (2026-04-23): contact (fully embedded)
# - employee_profile:1.0 (2026-04-23): employee, inquiry (many-to-many)

GROUPS = [
    {
        "name": "sales_pipeline",
        "tables": ["inquiry", "followup", "employee"],
        "link_notes": ["Links to: contact (FK, not embedded—contact_root handles contact schema)"],
        "schema_version": "sales_pipeline:1.0",
        "description": "Sales inquiry lifecycle: inquiry creation → followup cadence → employee assignment. Join contact via contact_id FK.",
    },
    {
        "name": "billing",
        "tables": ["invoice", "invoice_item", "payment"],
        "link_notes": ["Links to: contact (FK, not embedded—contact_root handles contact schema)"],
        "schema_version": "billing:1.0",
        "description": "Billing lifecycle: invoice creation → line items → payment records. Join contact via contact_id FK.",
    },
    {
        "name": "contact_root",
        "tables": ["contact"],
        "link_notes": [],
        "schema_version": "contact_root:1.0",
        "description": "Core contact entity; fully embedded. Single source of truth for all contact-related queries.",
    },
    {
        "name": "employee_profile",
        "tables": ["employee", "inquiry"],
        "link_notes": ["Inquiry references employee via assigned_to FK"],
        "schema_version": "employee_profile:1.0",
        "description": "Employee context: employee profiles + inquiries assigned to them. Useful for employee-scoped sales reports.",
    },
]


def get_schema_version(table_name: str) -> str:
    """Return schema_version for a specific table; used for chunk metadata."""
    if table_name not in SCHEMA:
        raise ValueError(f"Unknown table: {table_name}")
    return SCHEMA[table_name]["schema_version"]


def get_related_tables(table_name: str) -> list[str]:
    """Return list of tables related to the given table."""
    if table_name not in SCHEMA:
        raise ValueError(f"Unknown table: {table_name}")
    return SCHEMA[table_name]["related_to"]


def get_group(group_name: str) -> dict | None:
    """Return group definition by name; None if not found."""
    for group in GROUPS:
        if group["name"] == group_name:
            return group
    return None


def validate_registry() -> None:
    """
    Validate registry at module load time; raise ValueError if any issues found.
    
    Checks:
    - All tables referenced by groups exist in SCHEMA
    - All related_to references exist in SCHEMA
    - All group names are unique
    
    This function is called at module import; fail-fast pattern ensures
    config errors are caught immediately, not during ingest/retrieval.
    """
    errors = []
    
    # Check group table references
    for group in GROUPS:
        for table in group["tables"]:
            if table not in SCHEMA:
                errors.append(f"Group '{group['name']}' references missing table '{table}'")
    
    # Check related_to references
    for table_name, table_def in SCHEMA.items():
        for related_table in table_def["related_to"]:
            if related_table not in SCHEMA:
                errors.append(f"Table '{table_name}' has related_to reference to missing table '{related_table}'")
    
    # Check group name uniqueness
    group_names = [g["name"] for g in GROUPS]
    duplicates = [name for name in group_names if group_names.count(name) > 1]
    if duplicates:
        errors.append(f"Duplicate group names: {', '.join(set(duplicates))}")
    
    if errors:
        raise ValueError(f"Schema registry validation failed:\n" + "\n".join([f"  - {e}" for e in errors]))


# Validate at module import time
validate_registry()
```

### Verification
- ✅ File created with 8 tables, 4 groups
- ✅ Each table has columns, description, related_to, schema_version
- ✅ Each group has tables, link_notes, schema_version, description
- ✅ `validate_registry()` runs at import; raises if any issues
- ✅ Helper functions `get_schema_version()`, `get_related_tables()`, `get_group()` implemented
- ✅ CHANGELOG comments document each group and table

---

## Fix 2: Add Per-Group Versioning & Import-Time Validation

### File
**Update:** `nl2sql_service/schema_registry.py`

### Context
Per-group schema versions allow surgical schema updates without affecting other groups. When you change the `sales_pipeline` group (add a field to inquiry, for example), only chunks with `metadata.schema_version = "sales_pipeline:1.0"` will be refreshed. Other groups remain untouched.

The global `TABLE_SCHEMA_VERSION` tracks table definitions; per-group versions track group compositions. They are independent.

**Already included in Fix 1 above.** The file created in Fix 1 includes:
- `TABLE_SCHEMA_VERSION = "tables:1.0"` (global, for all table defs)
- Per-group `schema_version` in each GROUPS entry (e.g., `"sales_pipeline:1.0"`)
- `validate_registry()` function called at import time

---

## Fix 3: Apply Contact Deduplication in Chunker

### File
**Update:** `nl2sql_service/chunker.py`

### Purpose
Contact appears in multiple groups: `sales_pipeline` (via inquiry/followup), `billing` (via invoice/payment), and `employee_profile`. Embedding full contact schema in each group wastes vector budget and causes top-k collisions.

Solution: `sales_pipeline` and `billing` link to `contact` via FK reference in metadata; only `contact_root` embeds full contact schema.

### Implementation

Find the section in `chunker.py` where `chunk_schema_group()` is defined. Update `_render_group_text()` to apply link logic:

```python
def _render_group_text(group: dict) -> str:
    """Render embeddable text for schema group, applying contact deduplication."""
    from nl2sql_service.schema_registry import SCHEMA
    
    lines = [f"# {group['name'].replace('_', ' ').title()} Schema Group"]
    
    # Render each table in the group
    for table_name in group["tables"]:
        table = SCHEMA[table_name]
        lines.append(f"\n## Table: {table_name}")
        lines.append(f"Description: {table['description']}")
        lines.append(f"Columns: {', '.join(table['columns'])}")
    
    # Add link notes (FK references to deduped schemas)
    if group["link_notes"]:
        lines.append("\n## Related Schemas (Linked via FK):")
        for note in group["link_notes"]:
            lines.append(f"- {note}")
    
    # Add group description
    lines.append(f"\nGroup Description: {group.get('description', '')}")
    
    return "\n".join(lines)
```

### Verification
- ✅ `sales_pipeline` link_notes include: "Links to: contact (FK, not embedded—contact_root handles contact schema)"
- ✅ `billing` link_notes include: "Links to: contact (FK, not embedded—contact_root handles contact schema)"
- ✅ `contact_root` has empty link_notes (contact fully embedded)
- ✅ `employee_profile` link_notes include: "Inquiry references employee via assigned_to FK"
- ✅ Text output includes link_notes but NOT full contact schema for sales_pipeline/billing

---

## Fix 4: Add Token Count Guard

### File
**Update:** `nl2sql_service/chunker.py`

### Purpose
Prevent silent data truncation. If a group chunk exceeds 400 tokens (estimated), raise `ValueError` immediately so developers know the group is too large.

### Implementation

Update `chunk_schema_group()` function:

```python
def chunk_schema_group(group_name: str) -> dict:
    """
    Chunk a schema group into a single embeddable chunk.
    
    Returns dict with:
    - text: rendered schema text
    - source: f"schema_group:{group_name}"
    - metadata: type, tables, related_tables, group_description, schema_version
    
    Raises ValueError if token count exceeds 400 (estimated).
    """
    from nl2sql_service.schema_registry import get_group, get_related_tables
    
    group = get_group(group_name)
    if not group:
        raise ValueError(f"Unknown schema group: {group_name}")
    
    text = _render_group_text(group)
    
    # Estimate tokens: ~1.3 tokens per word
    word_count = len(text.split())
    estimated_tokens = int(word_count * 1.3)
    
    if estimated_tokens > 400:
        raise ValueError(
            f"Schema group '{group_name}' exceeds 400-token budget. "
            f"Estimated tokens: {estimated_tokens}. "
            f"Word count: {word_count}. "
            f"Please split this group or reduce table count."
        )
    
    # Build related_tables union (all tables referenced by tables in this group)
    related_tables = set()
    for table_name in group["tables"]:
        related_tables.update(get_related_tables(table_name))
    
    return {
        "text": text,
        "source": f"schema_group:{group_name}",
        "metadata": {
            "type": "schema_group",
            "tables": group["tables"],
            "related_tables": list(related_tables),
            "group_description": group.get("description", ""),
            "schema_version": group["schema_version"],
        },
    }
```

### Verification
- ✅ Token guard raises `ValueError` if > 400 tokens
- ✅ Error message includes estimated token count and word count
- ✅ Error message provides actionable guidance (split group or reduce table count)
- ✅ All 4 groups currently pass guard (verify by running script below)

**Test:**
```bash
cd /var/www/py-workspace/nl2sql
python3 -c "
from nl2sql_service.chunker import chunk_schema_group
for group in ['sales_pipeline', 'billing', 'contact_root', 'employee_profile']:
    try:
        chunk = chunk_schema_group(group)
        print(f'{group}: OK (estimated {chunk[\"metadata\"].get(\"token_count\", \"unknown\")} tokens)')
    except ValueError as e:
        print(f'{group}: FAILED - {e}')
"
```

---

## Fix 5: Implement Version-Aware Upsert

### File
**Update:** `nl2sql_service/ingest.py`

### Purpose
Current `ON CONFLICT DO NOTHING` silently skips chunks if they already exist, even if the schema has changed. Version-aware upsert uses `ON CONFLICT DO UPDATE` with a `WHERE schema_version !=` clause to auto-refresh stale chunks.

### Implementation

Find the `insert_chunks()` function in `db.py` (used by `ingest_schema_groups()`) and ensure it uses this SQL:

```python
async def insert_chunks(
    pool: asyncpg.Pool,
    chunks: list[dict],
    source: str,
) -> tuple[int, int]:
    """
    Insert chunks with version-aware upsert.
    
    Returns tuple (inserted_count, updated_count):
    - inserted_count: chunks newly inserted
    - updated_count: chunks updated due to schema_version mismatch
    
    Uses ON CONFLICT DO UPDATE with WHERE clause to auto-refresh stale chunks.
    Chunks with matching (source, chunk_index) are only updated if their
    schema_version differs from the new metadata.schema_version.
    """
    inserted_count = 0
    updated_count = 0
    
    for chunk in chunks:
        metadata_json = json.dumps(chunk["metadata"])
        embedding = chunk["embedding"]
        
        # Version-aware upsert: update only if schema_version changed
        result = await pool.execute(
            """
            INSERT INTO nl2sql_embeddings (source, chunk_index, content, embedding, metadata, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
            ON CONFLICT (source, chunk_index)
            DO UPDATE SET
                content = EXCLUDED.content,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
            WHERE
                metadata->>'schema_version' != EXCLUDED.metadata->>'schema_version'
            """,
            source,
            chunk.get("chunk_index", 0),
            chunk["text"],
            embedding,
            metadata_json,
        )
    
    # Parse result to determine inserted vs. updated
    # asyncpg result string format: "INSERT 0 1" or "UPDATE 1"
    result_str = result.strip()
    if result_str.startswith("INSERT"):
        inserted_count += 1
    elif result_str.startswith("UPDATE"):
        updated_count += 1
    
    return inserted_count, updated_count
```

### Verification
- ✅ SQL uses `ON CONFLICT DO UPDATE` with `WHERE schema_version !=` clause
- ✅ Counts inserted vs. updated separately
- ✅ Metadata includes schema_version (set by chunker)
- ✅ Test: ingest same group twice with different table content; second ingest should update chunks

**Test:**
```bash
# First ingest
curl -X POST http://localhost:8080/ingest/groups \
  -H "Content-Type: application/json" \
  -d '{"type": "groups", "group_names": ["sales_pipeline"]}'

# Expected: {"inserted": N, "updated": 0, "source": "schema_group:sales_pipeline"}

# Second ingest (same group, same schema_version)
curl -X POST http://localhost:8080/ingest/groups \
  -H "Content-Type: application/json" \
  -d '{"type": "groups", "group_names": ["sales_pipeline"]}'

# Expected: {"inserted": 0, "updated": 0, "source": "schema_group:sales_pipeline"}
# (No insert, no update—schema_version unchanged)
```

---

## Fix 6: Migrate to HNSW Index

### File
**Update:** `nl2sql_service/db.py` and `nl2sql_service/ingest.py`

### Purpose
IVFFlat requires manual `lists` tuning (~√row_count) and periodic `VACUUM ANALYZE`. HNSW is self-tuning with fixed hyperparameters (m=16, ef_construction=64), providing consistent recall without maintenance.

### db.py Changes

Update the `_DDL` constant to use HNSW index:

```python
_DDL = """
CREATE TABLE IF NOT EXISTS nl2sql_embeddings (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (source, chunk_index)
);

CREATE INDEX IF NOT EXISTS nl2sql_embeddings_hnsw_idx
    ON nl2sql_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

COMMENT ON INDEX nl2sql_embeddings_hnsw_idx IS 'HNSW index for semantic search. m=16 (neighbor count), ef_construction=64 (construction effort). No lists tuning required.';
"""
```

### ingest.py Changes

Add function to migrate from IVFFlat to HNSW:

```python
async def ensure_hnsw_index(pool: asyncpg.Pool) -> None:
    """
    Migrate from legacy IVFFlat index to HNSW.
    
    Steps:
    1. Drop IVFFlat index if exists (safe: queries still work on unindexed column)
    2. Create HNSW index with fixed hyperparameters (m=16, ef_construction=64)
    
    Safe to run multiple times (idempotent).
    """
    try:
        # Drop legacy IVFFlat index
        await pool.execute("DROP INDEX IF EXISTS nl2sql_embeddings_ivfflat_idx")
        logger.info("Dropped legacy IVFFlat index (if existed)")
    except Exception as e:
        logger.warning(f"Failed to drop IVFFlat index: {e}")
    
    try:
        # Create HNSW index
        await pool.execute(
            """
            CREATE INDEX IF NOT EXISTS nl2sql_embeddings_hnsw_idx
                ON nl2sql_embeddings
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
            """
        )
        logger.info("Created HNSW index (or already exists)")
    except Exception as e:
        logger.error(f"Failed to create HNSW index: {e}")
        raise
```

### main.py Changes

Call `ensure_hnsw_index()` in lifespan after bootstrap:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """App lifespan: startup → serve → shutdown."""
    # Startup
    app.state.db_pool = await db.create_pool()
    await db.bootstrap(app.state.db_pool)
    await ingest.ensure_hnsw_index(app.state.db_pool)  # Migrate to HNSW
    logger.info("HNSW index migration complete")
    
    yield
    
    # Shutdown
    await app.state.db_pool.close()
    logger.info("DB pool closed")
```

### Verification
- ✅ Legacy IVFFlat index dropped on startup (if exists)
- ✅ HNSW index created with fixed m=16, ef_construction=64
- ✅ No manual `lists` tuning required
- ✅ Query performance unaffected

**Test:**
```bash
# After deployment, verify index type
psql ragdb -c "
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'nl2sql_embeddings'
ORDER BY indexname;
"

# Expected output:
# nl2sql_embeddings_hnsw_idx | CREATE INDEX nl2sql_embeddings_hnsw_idx ON public.nl2sql_embeddings USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)
```

---

## Fix 7: Implement ingest_schema_groups()

### File
**Update:** `nl2sql_service/ingest.py`

### Purpose
High-level function to ingest one or more schema groups. Orchestrates chunk → embed → insert pipeline and returns counts.

### Implementation

```python
async def ingest_schema_groups(
    group_names: list[str] | None = None,
    pool: asyncpg.Pool | None = None,
) -> tuple[int, int]:
    """
    Ingest schema groups.
    
    Args:
        group_names: List of group names to ingest (e.g., ["sales_pipeline", "billing"]).
                     If None, ingest all groups from schema_registry.GROUPS.
        pool: asyncpg connection pool. If None, create temporary pool.
    
    Returns:
        tuple: (inserted_count, updated_count)
    
    Raises:
        ValueError: If group_names contains unknown groups or if any chunk exceeds token budget.
    """
    from nl2sql_service.schema_registry import GROUPS
    from nl2sql_service.chunker import chunk_schema_group
    
    # Resolve group names
    if group_names is None:
        group_names = [g["name"] for g in GROUPS]
    
    # Validate group names exist
    valid_group_names = {g["name"] for g in GROUPS}
    invalid = set(group_names) - valid_group_names
    if invalid:
        raise ValueError(f"Unknown groups: {', '.join(invalid)}")
    
    # Create pool if not provided
    close_pool = False
    if pool is None:
        pool = await create_pool()
        close_pool = True
    
    try:
        total_inserted = 0
        total_updated = 0
        
        for group_name in group_names:
            logger.info(f"Ingesting schema group: {group_name}")
            
            # Chunk group (raises ValueError if > 400 tokens)
            chunk = chunk_schema_group(group_name)
            
            # Embed text
            embeddings = await embed.embed_texts([chunk["text"]])
            if not embeddings:
                logger.warning(f"Failed to embed {group_name}")
                continue
            
            chunk["embedding"] = embeddings[0]
            
            # Insert with version-aware upsert
            inserted, updated = await insert_chunks(pool, [chunk], chunk["source"])
            total_inserted += inserted
            total_updated += updated
            
            logger.info(f"  {group_name}: inserted={inserted}, updated={updated}")
        
        return total_inserted, total_updated
    
    finally:
        if close_pool:
            await pool.close()
```

### Verification
- ✅ Accepts group_names list or None (all groups)
- ✅ Returns tuple(inserted_count, updated_count)
- ✅ Calls chunk_schema_group() (validates token budget)
- ✅ Calls embed_texts() to get embeddings
- ✅ Calls insert_chunks() for version-aware upsert
- ✅ Logs progress for each group

---

## Fix 8: Add GroupQueryResponse Model

### File
**Update:** `nl2sql_service/models.py`

### Purpose
Response model for schema-group-aware queries.

### Implementation

Add to `models.py`:

```python
from pydantic import BaseModel
from typing import Literal

class GroupQueryResponse(BaseModel):
    """Response from /query/groups endpoint."""
    matched_groups: list[str]  # Groups with chunks above similarity threshold
    tables_in_scope: list[str]  # Union of all tables in matched groups + their related_tables
    context: str  # Formatted multi-group schema text for LLM context
    results: list[QueryResult]  # Raw query results


class IngestGroupsRequest(BaseModel):
    """Request to /ingest/groups endpoint."""
    type: Literal["groups"]
    group_names: list[str] | None = None  # If None, ingest all groups


class IngestResponse(BaseModel):
    """Response from /ingest/* endpoints."""
    inserted: int
    updated: int = 0  # For schema-group upsert
    source: str
```

### Verification
- ✅ `GroupQueryResponse` includes matched_groups, tables_in_scope, context, results
- ✅ `IngestGroupsRequest` includes type and optional group_names
- ✅ `IngestResponse` includes inserted, updated, source

---

## Fix 9: Implement retrieve_groups()

### File
**Update:** `nl2sql_service/retrieve.py`

### Purpose
Query the vector DB and return schema-group-specific results (matched groups, tables in scope, formatted context).

### Implementation

```python
async def retrieve_groups(
    query: str,
    top_k: int = 5,
    pool: asyncpg.Pool | None = None,
) -> dict:
    """
    Retrieve schema groups matching query.
    
    Returns dict with:
    - matched_groups: list of group names with chunks above threshold
    - tables_in_scope: union of tables in matched groups + related_tables
    - context: formatted schema text for LLM
    - results: list of QueryResult (raw results)
    """
    from nl2sql_service.models import GroupQueryResponse
    from nl2sql_service.schema_registry import SCHEMA, get_group
    
    # Create pool if not provided
    close_pool = False
    if pool is None:
        pool = await create_pool()
        close_pool = True
    
    try:
        # Generic retrieve (all chunk types)
        results = await retrieve(query, top_k, pool)
        
        # Filter to schema_group type only
        group_results = [r for r in results if r.get("metadata", {}).get("type") == "schema_group"]
        
        # Extract unique matched groups
        matched_groups = list(set(r.get("metadata", {}).get("group_name") for r in group_results))
        matched_groups = [g for g in matched_groups if g]  # Remove None
        
        # Build tables_in_scope (union of tables + related_tables)
        tables_in_scope = set()
        for result in group_results:
            metadata = result.get("metadata", {})
            tables_in_scope.update(metadata.get("tables", []))
            tables_in_scope.update(metadata.get("related_tables", []))
        
        # Format context: multi-group schema text
        context_lines = ["# Matched Schema Groups\n"]
        for group_name in matched_groups:
            group = get_group(group_name)
            if group:
                context_lines.append(f"\n## {group['name']}")
                context_lines.append(group.get("description", ""))
                context_lines.append(f"Tables: {', '.join(group['tables'])}")
        
        context = "\n".join(context_lines)
        
        return {
            "matched_groups": matched_groups,
            "tables_in_scope": sorted(tables_in_scope),
            "context": context,
            "results": results,
        }
    
    finally:
        if close_pool:
            await pool.close()
```

### Verification
- ✅ Filters results to metadata.type='schema_group'
- ✅ Extracts matched_groups from results
- ✅ Builds tables_in_scope union
- ✅ Formats multi-group context text

---

## Fix 10: Add New Endpoints

### File
**Update:** `nl2sql_service/main.py`

### Purpose
Expose /ingest/groups and /query/groups endpoints.

### Implementation

```python
from fastapi import FastAPI, HTTPException
from nl2sql_service.models import IngestGroupsRequest, GroupQueryResponse, QueryRequest

@app.post("/ingest/groups")
async def ingest_groups(req: IngestGroupsRequest):
    """
    Ingest schema groups.
    
    Request:
    {
      "type": "groups",
      "group_names": ["sales_pipeline", "billing"]  # optional; if null, ingest all
    }
    
    Response:
    {
      "inserted": 2,
      "updated": 0,
      "source": "schema_group:*"
    }
    """
    try:
        inserted, updated = await ingest.ingest_schema_groups(
            group_names=req.group_names,
            pool=app.state.db_pool,
        )
        return {
            "inserted": inserted,
            "updated": updated,
            "source": "schema_group:*",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/query/groups")
async def query_groups(req: QueryRequest) -> GroupQueryResponse:
    """
    Query schema groups matching request.
    
    Request:
    {
      "query": "What sales inquiries are pending?"
    }
    
    Response:
    {
      "matched_groups": ["sales_pipeline"],
      "tables_in_scope": ["inquiry", "followup", "employee", "contact"],
      "context": "# Matched Schema Groups\n\n## Sales Pipeline\n...",
      "results": [...]
    }
    """
    try:
        response = await retrieve.retrieve_groups(
            query=req.query,
            top_k=req.top_k or 5,
            pool=app.state.db_pool,
        )
        return GroupQueryResponse(**response)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

### Verification
- ✅ POST /ingest/groups accepts IngestGroupsRequest
- ✅ POST /query/groups accepts QueryRequest
- ✅ Both endpoints return correct response models
- ✅ Error handling with HTTPException

---

## Fix 11: Refactor Ingest Script

### File
**Update:** `scripts/nl2sql_ingest_groups.py`

### Purpose
Replace hardcoded group payloads with registry-driven dynamic payload generation.

### Implementation

```python
#!/usr/bin/env python3
"""
Ingest schema groups via FastAPI /ingest/groups endpoint.

Uses schema_registry.GROUPS as source of truth (no hardcoding).

Usage:
  python scripts/nl2sql_ingest_groups.py --url http://localhost:8080
  python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --groups sales_pipeline billing
"""

import argparse
import asyncio
import httpx
import sys
from nl2sql_service.schema_registry import GROUPS


async def main():
    parser = argparse.ArgumentParser(description="Ingest schema groups")
    parser.add_argument("--url", default="http://localhost:8080", help="FastAPI base URL")
    parser.add_argument("--groups", nargs="*", help="Group names to ingest (space-separated)")
    args = parser.parse_args()
    
    # Resolve group names
    if args.groups:
        group_names = args.groups
        # Validate
        valid_names = {g["name"] for g in GROUPS}
        invalid = set(group_names) - valid_names
        if invalid:
            print(f"ERROR: Unknown groups: {', '.join(invalid)}", file=sys.stderr)
            sys.exit(1)
    else:
        group_names = [g["name"] for g in GROUPS]
    
    print(f"Ingesting groups: {', '.join(group_names)}")
    
    # Send request
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{args.url}/ingest/groups",
            json={"type": "groups", "group_names": group_names},
        )
        
        if response.status_code == 200:
            result = response.json()
            print(f"SUCCESS: inserted={result['inserted']}, updated={result['updated']}")
            sys.exit(0)
        else:
            print(f"ERROR: {response.status_code} {response.text}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
```

### Verification
- ✅ Imports GROUPS from schema_registry (no hardcoding)
- ✅ Accepts --url and --groups arguments
- ✅ Defaults to all groups if --groups not specified
- ✅ Validates group names exist

---

## Fix 12: Update README

### File
**Update:** `README.md`

### Changes to Add

#### Schema Registry Pattern Section

```markdown
## Schema Registry Pattern

All table and schema-group definitions are centralized in `nl2sql_service/schema_registry.py`.

**Adding a new table or group requires ONLY editing this file.**

Other files (chunker.py, ingest.py, models.py, etc.) import from schema_registry and apply generic logic.

### Example: Adding support_tickets Group

1. Add support_tickets table to SCHEMA:
   ```python
   "support_tickets": {
       "columns": [...],
       "description": "...",
       "related_to": ["contact", "employee"],
       "schema_version": "tables:1.0",
   }
   ```

2. Add support_tickets group to GROUPS:
   ```python
   {
       "name": "support_tickets",
       "tables": ["support_tickets", "contact", "employee"],
       "link_notes": [],
       "schema_version": "support_tickets:1.0",
       "description": "...",
   }
   ```

3. Ingest immediately:
   ```bash
   python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --groups support_tickets
   ```

That's it. No other files need changes.
```

#### Per-Group Versioning Section

```markdown
## Per-Group Schema Versioning

Each schema group has an independent `schema_version` (e.g., `sales_pipeline:1.0`, `billing:1.0`).

When you update a table in the `sales_pipeline` group (e.g., add a column to inquiry):

1. Update the table definition in SCHEMA (increment schema_version if shared across groups)
2. Update the group schema_version in GROUPS (e.g., `sales_pipeline:1.1`)
3. Ingest: `python scripts/nl2sql_ingest_groups.py --groups sales_pipeline`

The version-aware upsert (ON CONFLICT DO UPDATE WHERE schema_version !=) ensures only `sales_pipeline` chunks are refreshed. Other groups unaffected.

### Version Bump Guidance

- **Global table definition change** (affects multiple groups): Increment TABLE_SCHEMA_VERSION, bump all group versions that use the table
- **Single-group change**: Bump only that group's schema_version
- **Non-breaking change** (e.g., add optional column): Can keep version same or bump to signal SDK migration
- **Breaking change** (e.g., remove/rename column): Always bump version; tests may fail until client code updated
```

#### HNSW Index Section

```markdown
## HNSW Vector Index

The service uses HNSW (Hierarchical Navigable Small World) for semantic search.

**Why HNSW over IVFFlat?**
- **No lists tuning**: IVFFlat requires `lists ≈ √row_count` and periodic VACUUM ANALYZE. HNSW has fixed m=16, ef_construction=64.
- **Consistent recall**: HNSW provides predictable recall across different dataset sizes. IVFFlat degrades without maintenance.
- **Production-safe**: HNSW is the recommended index for pgvector in production.

On startup, `ensure_hnsw_index()` drops any legacy IVFFlat index and creates HNSW (idempotent).
```

#### New Endpoints Section

```markdown
## API Endpoints

### POST /ingest/groups

Ingest schema groups.

**Request:**
```json
{
  "type": "groups",
  "group_names": ["sales_pipeline", "billing"]
}
```

If `group_names` is null, all groups are ingested.

**Response:**
```json
{
  "inserted": 2,
  "updated": 0,
  "source": "schema_group:*"
}
```

### POST /query/groups

Query schema groups matching query.

**Request:**
```json
{
  "query": "What sales inquiries are pending?"
}
```

**Response:**
```json
{
  "matched_groups": ["sales_pipeline"],
  "tables_in_scope": ["inquiry", "followup", "employee", "contact"],
  "context": "# Matched Schema Groups\n\n## Sales Pipeline\n...",
  "results": [...]
}
```
```

### Verification
- ✅ Schema Registry Pattern section with support_tickets example
- ✅ Per-Group Versioning section with version bump guidance
- ✅ HNSW Index section with rationale
- ✅ New Endpoints section with request/response examples

---

## Fix 13: Verification & Testing

### Prerequisites
- FastAPI service running at http://localhost:8080
- PostgreSQL + pgvector at :5432/ragdb
- TEI embedding API at http://localhost:8000/embed

### Integration Test Suite

**1. Initialize Service**

```bash
# Start service (from workspace root)
python -m nl2sql_service.main &
sleep 5
echo "Service started"
```

**2. Verify Schema Registry**

```bash
# Import registry; should pass validation
python3 -c "
from nl2sql_service.schema_registry import validate_registry, GROUPS, SCHEMA
print(f'Registry valid. Tables: {len(SCHEMA)}, Groups: {len(GROUPS)}')
for group in GROUPS:
    print(f'  {group[\"name\"]}: {group[\"schema_version\"]}')
"

# Expected:
# Registry valid. Tables: 8, Groups: 4
#   sales_pipeline: sales_pipeline:1.0
#   billing: billing:1.0
#   contact_root: contact_root:1.0
#   employee_profile: employee_profile:1.0
```

**3. Verify Token Guard**

```bash
# All groups should pass
python3 -c "
from nl2sql_service.chunker import chunk_schema_group
for group in ['sales_pipeline', 'billing', 'contact_root', 'employee_profile']:
    chunk = chunk_schema_group(group)
    print(f'{group}: {chunk[\"metadata\"].get(\"schema_version\")}, tables={chunk[\"metadata\"][\"tables\"]}')
"

# Expected:
# sales_pipeline: sales_pipeline:1.0, tables=['inquiry', 'followup', 'employee']
# billing: billing:1.0, tables=['invoice', 'invoice_item', 'payment']
# contact_root: contact_root:1.0, tables=['contact']
# employee_profile: employee_profile:1.0, tables=['employee', 'inquiry']
```

**4. Test /ingest/groups Endpoint**

```bash
# Ingest all groups
curl -X POST http://localhost:8080/ingest/groups \
  -H "Content-Type: application/json" \
  -d '{"type": "groups"}'

# Expected: {"inserted": 4, "updated": 0, "source": "schema_group:*"}

# Ingest again (no changes)
curl -X POST http://localhost:8080/ingest/groups \
  -H "Content-Type: application/json" \
  -d '{"type": "groups"}'

# Expected: {"inserted": 0, "updated": 0, "source": "schema_group:*"}

# Ingest specific group
curl -X POST http://localhost:8080/ingest/groups \
  -H "Content-Type: application/json" \
  -d '{"type": "groups", "group_names": ["sales_pipeline"]}'

# Expected: {"inserted": 0, "updated": 0, "source": "schema_group:*"}
# (already ingested with same schema_version)
```

**5. Test /query/groups Endpoint**

```bash
# Query for sales inquiries
curl -X POST http://localhost:8080/query/groups \
  -H "Content-Type: application/json" \
  -d '{"query": "What sales inquiries are pending?"}'

# Expected:
# {
#   "matched_groups": ["sales_pipeline"],
#   "tables_in_scope": ["contact", "employee", "followup", "inquiry"],
#   "context": "# Matched Schema Groups\n\n## Sales Pipeline\n...",
#   "results": [...]
# }
```

**6. Test Single-File Pattern (support_tickets)**

```bash
# 1. Add support_tickets to schema_registry.py SCHEMA dict:
#    (table def here)

# 2. Add support_tickets group to schema_registry.py GROUPS:
#    (group def here)

# 3. Run:
python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --groups support_tickets

# Expected: SUCCESS: inserted=1, updated=0

# 4. Query:
curl -X POST http://localhost:8080/query/groups \
  -H "Content-Type: application/json" \
  -d '{"query": "Are there open support tickets?"}'

# Expected: "matched_groups" includes "support_tickets"

# Verification: Only schema_registry.py was edited. No other files changed.
✅ Single-file pattern confirmed.
```

**7. Verify HNSW Index**

```bash
# After /ingest/groups, verify index type
psql ragdb -c "
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'nl2sql_embeddings'
ORDER BY indexname;
"

# Expected:
# nl2sql_embeddings_hnsw_idx | CREATE INDEX ... USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)
```

**8. Test Version-Aware Upsert**

```bash
# 1. Ingest all groups
curl -X POST http://localhost:8080/ingest/groups \
  -H "Content-Type: application/json" \
  -d '{"type": "groups"}' -w "\n"
# Response: {"inserted": 4, "updated": 0}

# 2. Bump sales_pipeline version in schema_registry.py
#    Change "sales_pipeline": "sales_pipeline:1.0" to "sales_pipeline": "sales_pipeline:1.1"

# 3. Ingest again
curl -X POST http://localhost:8080/ingest/groups \
  -H "Content-Type: application/json" \
  -d '{"type": "groups", "group_names": ["sales_pipeline"]}' -w "\n"
# Response: {"inserted": 0, "updated": 1}
# (Version mismatch triggered update, not insert)
```

### Test Results Checklist

- [ ] Schema registry imports without error
- [ ] All 4 groups have correct schema_version
- [ ] All groups pass token guard (no ValueError)
- [ ] /ingest/groups returns correct inserted/updated counts
- [ ] /ingest/groups idempotent on second run
- [ ] /query/groups returns matched_groups and tables_in_scope
- [ ] support_tickets example requires ONLY schema_registry.py edits
- [ ] HNSW index created (not IVFFlat)
- [ ] Version-aware upsert updates chunks on schema_version mismatch

---

## Rollback Plan

If anything breaks:

1. **Schema Registry Syntax Error**: Fix error in schema_registry.py; re-run service startup
2. **Token Guard Exceeded**: Reduce table count in group or split into two groups
3. **Upsert Error**: Verify metadata JSON is valid; check schema_version format
4. **Index Creation Failed**: Ensure pgvector extension installed; check Postgres version >= 15
5. **Endpoint Error**: Verify imports in main.py, ingest.py, retrieve.py; check log output

### Restore Previous Version

```bash
# Backup current schema_registry.py
cp nl2sql_service/schema_registry.py nl2sql_service/schema_registry.py.backup

# Restore from git (if available)
git checkout nl2sql_service/schema_registry.py

# Restart service
pkill -f "python -m nl2sql_service.main"
python -m nl2sql_service.main &
```

---

## Summary

This plan implements 6 critical fixes to the NL2SQL RAG service:

1. ✅ **Fix 1**: Create `schema_registry.py` with single source of truth
2. ✅ **Fix 2**: Add per-group versioning + import-time validation
3. ✅ **Fix 3**: Apply contact deduplication in chunker
4. ✅ **Fix 4**: Add token count guard (prevents > 400 tokens)
5. ✅ **Fix 5**: Implement version-aware upsert (ON CONFLICT DO UPDATE)
6. ✅ **Fix 6**: Migrate to HNSW index (safer, no lists tuning)

Plus:

- ✅ New endpoints `/ingest/groups` and `/query/groups`
- ✅ Refactored ingest script (registry-driven, no hardcoding)
- ✅ Updated README with patterns and guidance
- ✅ Comprehensive verification steps with curl examples
- ✅ Single-file pattern confirmed (support_tickets example)

**Total Implementation Time**: ~2 hours (120 minutes) following the 13-step sequence.

**Key Validation**: After completing all steps, run the integration test suite above to confirm all fixes working correctly.

---

## File Checklist

Before starting implementation, verify these files exist:

- [ ] `/var/www/py-workspace/nl2sql/nl2sql_service/schema_registry.py` (NEW)
- [ ] `/var/www/py-workspace/nl2sql/nl2sql_service/chunker.py` (UPDATE)
- [ ] `/var/www/py-workspace/nl2sql/nl2sql_service/ingest.py` (UPDATE)
- [ ] `/var/www/py-workspace/nl2sql/nl2sql_service/retrieve.py` (UPDATE)
- [ ] `/var/www/py-workspace/nl2sql/nl2sql_service/models.py` (UPDATE)
- [ ] `/var/www/py-workspace/nl2sql/nl2sql_service/main.py` (UPDATE)
- [ ] `/var/www/py-workspace/nl2sql/nl2sql_service/db.py` (UPDATE)
- [ ] `/var/www/py-workspace/nl2sql/scripts/nl2sql_ingest_groups.py` (UPDATE)
- [ ] `/var/www/py-workspace/nl2sql/README.md` (UPDATE)

---

**Ready for implementation. Good luck! 🚀**
