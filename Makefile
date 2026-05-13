.PHONY: setup run test ingest benchmark smoke smoke-report sync-schema

# ── Setup ──────────────────────────────────────────────────────────────────
setup:
	python -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# ── Development server ─────────────────────────────────────────────────────
run:
	./.venv/bin/uvicorn nl2sql_service.main:app \
		--host 0.0.0.0 --port 8080 --reload

# ── Tests ──────────────────────────────────────────────────────────────────
test:
	./.venv/bin/pytest tests/ -v

# ── Ingest all schema groups ───────────────────────────────────────────────
ingest:
	./.venv/bin/python scripts/nl2sql_ingest_groups.py \
		--url http://localhost:8080 --all --timeout 600

# ── Replay benchmark (writes timestamped JSON report) ─────────────────────
benchmark:
	mkdir -p reports
	./.venv/bin/python scripts/nl2sql_replay_benchmark.py \
		--url http://localhost:8080 \
		--output reports/replay-$$(date +%F).json

# ── Full-route smoke matrix ────────────────────────────────────────────────
smoke:
	./.venv/bin/python scripts/nl2sql_smoke_test.py --url http://localhost:8080

smoke-report:
	mkdir -p reports
	./.venv/bin/python scripts/nl2sql_smoke_test.py \
		--url http://localhost:8080 \
		--output reports/smoke-$$(date +%F_%H%M%S).json

# ── Regenerate rag_schema/, sync, and re-ingest ───────────────────────────
sync-schema:
	bash scripts/sync_rag_schema.sh
