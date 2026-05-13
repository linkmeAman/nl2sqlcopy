#!/usr/bin/env bash
# =============================================================================
# sync_rag_schema.sh
# =============================================================================
# Regenerates the rag_schema/ JSON files from the PHP app, syncs them to the
# NL2SQL service directory, and triggers a full re-ingest.
#
# Run manually:
#   bash /var/www/py-workspace/nl2sql/scripts/sync_rag_schema.sh
#
# Wire as a PHP post-deploy hook (fire-and-forget, non-blocking):
#   exec('/var/www/py-workspace/nl2sql/scripts/sync_rag_schema.sh > /tmp/nl2sql_sync.log 2>&1 &');
#
# Symlink alternative (permanent fix — removes the rsync step forever):
#   rm -rf /var/www/py-workspace/nl2sql/rag_schema
#   ln -s /var/www/developer.tickleright.in/rag_schema /var/www/py-workspace/nl2sql/rag_schema
#   After symlinking, the script still works but the rsync step is a no-op.
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# CONFIG — edit these three values once, then leave them alone
# ---------------------------------------------------------------------------
PHP_APP_DIR="/var/www/developer.tickleright.in"
NL2SQL_DIR="/var/www/py-workspace/nl2sql"
NL2SQL_URL="http://localhost:8080"
# ---------------------------------------------------------------------------

VENV_PYTHON="${NL2SQL_DIR}/.venv/bin/python"
PHP_SCRIPT="${PHP_APP_DIR}/scripts/rag_schema_build.php"
PHP_RAG_SCHEMA="${PHP_APP_DIR}/rag_schema"
NL2SQL_RAG_SCHEMA="${NL2SQL_DIR}/rag_schema"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

log "=== rag_schema sync started ==="

# Step 1: Regenerate rag_schema/ from PHP source
log "Step 1/3: Running rag_schema_build.php ..."
php "${PHP_SCRIPT}" --quiet
log "Step 1/3: Done."

# Step 2: Sync generated files to NL2SQL working directory
# (Skip if rag_schema is already a symlink pointing to the PHP output dir)
if [ -L "${NL2SQL_RAG_SCHEMA}" ]; then
    log "Step 2/3: Skipped — rag_schema is a symlink, no sync needed."
else
    log "Step 2/3: Syncing ${PHP_RAG_SCHEMA}/ → ${NL2SQL_RAG_SCHEMA}/ ..."
    rsync -a --delete "${PHP_RAG_SCHEMA}/" "${NL2SQL_RAG_SCHEMA}/"
    log "Step 2/3: Done."
fi

# Step 3: Re-ingest all schema groups and knowledge into NL2SQL
log "Step 3/3: Triggering full re-ingest (--all) ..."
"${VENV_PYTHON}" "${NL2SQL_DIR}/scripts/nl2sql_ingest_groups.py" \
    --url "${NL2SQL_URL}" \
    --all \
    --timeout 600
log "Step 3/3: Done."

log "=== rag_schema sync complete ==="
