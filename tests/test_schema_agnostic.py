from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nl2sql_service import query_rewriter, react_agent, rulebook, schema_loader, sql_generator, synonym_map
from nl2sql_service.models import GenerateSqlSuccess, QueryResult


MOCK_SCHEMA_SQL = """
CREATE TABLE product (
  id INT PRIMARY KEY,
  title VARCHAR(255),
  sku VARCHAR(100),
  unit_price DECIMAL(10,2),
  stock_qty INT
);

CREATE TABLE warehouse_order (
  id INT PRIMARY KEY,
  product_id INT,
  ordered_qty INT,
  shipped_at DATETIME,
  status VARCHAR(50)
);

CREATE TABLE supplier (
  id INT PRIMARY KEY,
  company_name VARCHAR(255),
  contact_email VARCHAR(255),
  region VARCHAR(100)
);
""".strip()

MOCK_SCHEMA_COLUMNS = {
    "product": ["id", "title", "sku", "unit_price", "stock_qty"],
    "warehouse_order": ["id", "product_id", "ordered_qty", "shipped_at", "status"],
    "supplier": ["id", "company_name", "contact_email", "region"],
}

PRODUCTION_SCHEMA_TERMS = [
    "fname",
    "lname",
    "fullname",
    "email",
    "mobile",
    "counselor",
    "invoice",
    "payment",
    "enrollment",
    "student",
]

PRODUCTION_TABLE_TERMS = [
    "invoice",
    "payment",
    "enrollment",
    "student",
]

GOVERNANCE_FORBIDDEN_TERMS = [
    "invoice",
    "payment",
    "enrollment",
    "counselor",
    "student",
    "fname",
    "lname",
]


def _assert_no_forbidden_terms(text: str, terms: list[str], origin: str) -> None:
    lowered = text.lower()
    for term in terms:
        if term.lower() in lowered:
            raise AssertionError(
                f"Hardcoded term '{term}' found in {origin}: {text}"
            )


def _all_tokens_derived_from_column(alias: str, column_name: str) -> bool:
    source_tokens = set(synonym_map.split_identifier_parts(column_name))
    alias_tokens = set(alias.lower().split())
    return alias_tokens.issubset(source_tokens)


def _assert_only_mock_columns(columns_by_table: dict[str, list[str]], origin: str) -> None:
    for table_name, columns in columns_by_table.items():
        expected = MOCK_SCHEMA_COLUMNS.get(table_name)
        if expected is None:
            raise AssertionError(
                f"Unexpected table '{table_name}' found in {origin}; expected only mock schema tables."
            )
        extra = sorted(set(columns) - set(expected))
        if extra:
            raise AssertionError(
                f"Unexpected columns {extra} found in {origin} for table '{table_name}'."
            )


def _temp_synonym_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from nl2sql_service.config import settings

    payload = json.loads(Path(settings.query_rewrite_synonym_map).read_text(encoding="utf-8"))
    payload.setdefault("query_terms", {})
    payload["query_terms"]["stock"] = ["quantity", "inventory", "available qty"]
    payload["query_terms"]["supplier"] = ["vendor", "manufacturer", "source"]

    path = tmp_path / "synonyms.test.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    synonym_map._load_raw.cache_clear()
    monkeypatch.setattr(settings, "query_rewrite_synonym_map", str(path))
    return path


@pytest.mark.asyncio
async def test_introspection_enrichment_produces_no_hardcoded_columns(
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service.config import settings

    mock_records = [
        {"table_name": table_name, "column_name": column_name, "data_type": "varchar", "ordinal_position": index}
        for table_name, columns in MOCK_SCHEMA_COLUMNS.items()
        for index, column_name in enumerate(columns, start=1)
    ]
    monkeypatch.setattr(
        schema_loader,
        "load_column_catalog",
        AsyncMock(return_value=mock_records),
    )

    chunks = await schema_loader.load_live_column_catalog_chunks(settings)

    assert chunks, "Expected schema_loader.load_live_column_catalog_chunks() to return mock schema chunks."
    produced = {
        str(chunk["table_name"]): [str(chunk["column_name"])]
        for chunk in chunks
    }
    _assert_only_mock_columns(produced, "schema_loader.load_live_column_catalog_chunks")
    for chunk in chunks:
        column_name = str(chunk["column_name"])
        for alias in chunk.get("aliases", []):
            # This is stronger than a static blocklist: it rejects any alias token
            # that cannot be derived from the column name itself, while allowing
            # legitimate split-based aliases such as "email" from "contact_email".
            assert _all_tokens_derived_from_column(str(alias), column_name), (
                f"Alias '{alias}' contains token not derivable from "
                f"column '{column_name}' - possible vocabulary leak"
            )


@pytest.mark.asyncio
async def test_synonym_expansion_works_for_new_domain_terms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service.config import settings

    _temp_synonym_map(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "query_rewrite_enabled", True)
    monkeypatch.setattr(
        query_rewriter,
        "build_rewrite_hints",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        query_rewriter,
        "_call_rewrite_model",
        AsyncMock(return_value="show me low stock products"),
    )

    rewritten = await query_rewriter.rewrite_search_query(
        "show me low stock products",
        pool=object(),
        settings=settings,
    )

    assert any(term in rewritten for term in ("quantity", "inventory", "available qty")), (
        "Expected query_rewriter.rewrite_search_query to append at least one stock synonym "
        f"from the test synonym map, got: {rewritten}"
    )
    _assert_no_forbidden_terms(
        rewritten,
        PRODUCTION_SCHEMA_TERMS,
        "query_rewriter.rewrite_search_query",
    )


@pytest.mark.asyncio
async def test_retrieve_schema_for_tables_uses_retrieval_not_hardcoding(
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service.config import settings

    state = {
        "context": "",
        "tables_in_scope": [],
        "matched_groups": [],
        "allowed_columns": {},
        "retrieved_schema": {},
        "retrieved_tables": set(),
        "top_k": 5,
        "search_query": "products with low stock",
    }

    monkeypatch.setattr(
        react_agent,
        "retrieve_groups",
        AsyncMock(
            return_value={
                "matched_groups": ["inventory_products"],
                "tables_in_scope": ["product"],
                "context": "Group: inventory_products\nTables: product",
                "results": [],
            }
        ),
    )
    monkeypatch.setattr(
        react_agent,
        "retrieve_column_catalog",
        AsyncMock(
            return_value=[
                QueryResult(
                    content=f"Table: product\nColumn: {column_name}",
                    similarity=0.95,
                    metadata={
                        "type": "column_catalog",
                        "table_name": "product",
                        "column_name": column_name,
                    },
                )
                for column_name in MOCK_SCHEMA_COLUMNS["product"]
            ]
        ),
    )

    observation, warnings = await react_agent.execute_action(
        action=react_agent.ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
        action_input="product",
        query="products with low stock",
        pool=object(),
        settings=settings,
        state=state,
    )

    assert warnings == []
    assert "column-level retrieval" in observation
    assert state["allowed_columns"] == {"product": MOCK_SCHEMA_COLUMNS["product"]}
    _assert_only_mock_columns(state["allowed_columns"], "react_agent.execute_action")
    _assert_no_forbidden_terms(
        json.dumps(state["allowed_columns"], sort_keys=True),
        PRODUCTION_SCHEMA_TERMS,
        "react_agent.execute_action",
    )


@pytest.mark.asyncio
async def test_sql_generation_targets_mock_schema_tables(
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service.config import settings

    monkeypatch.setattr(react_agent, "retrieve_past_corrections", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        react_agent,
        "retrieve_groups",
        AsyncMock(
            return_value={
                "matched_groups": ["inventory_suppliers"],
                "tables_in_scope": ["supplier"],
                "context": "Group: inventory_suppliers\nTables: supplier",
                "results": [],
            }
        ),
    )
    monkeypatch.setattr(
        react_agent,
        "retrieve_column_catalog",
        AsyncMock(
            return_value=[
                QueryResult(
                    content=f"Table: supplier\nColumn: {column_name}",
                    similarity=0.96,
                    metadata={
                        "type": "column_catalog",
                        "table_name": "supplier",
                        "column_name": column_name,
                    },
                )
                for column_name in MOCK_SCHEMA_COLUMNS["supplier"]
            ]
        ),
    )
    monkeypatch.setattr(
        react_agent,
        "call_reasoning_model",
        AsyncMock(
            side_effect=[
                (
                    "Supplier schema is enough to generate SQL.",
                    "ACTION: GENERATE_SQL\nINPUT: generate supplier sql",
                    [],
                ),
                (
                    "Validate the supplier SQL.",
                    "ACTION: VALIDATE_AND_RETURN\nINPUT: validate current sql",
                    [],
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        react_agent,
        "call_ollama",
        AsyncMock(
            return_value=(
                "SELECT id, company_name, region FROM supplier WHERE region = 'north'",
                [],
            )
        ),
    )
    monkeypatch.setattr(react_agent, "run_explain", AsyncMock(return_value=[]))

    result = await react_agent.run(
        query="show me all suppliers in the north region",
        pool=object(),
        settings=settings,
        top_k=5,
    )

    assert result.status == "ok"
    assert "supplier" in result.sql.lower()
    assert "region" in result.sql.lower()
    _assert_no_forbidden_terms(
        result.sql,
        PRODUCTION_TABLE_TERMS,
        "react_agent.run",
    )


@pytest.mark.asyncio
async def test_full_ask_pipeline_on_mock_schema(
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service import answer_generator, main, mysql_executor

    sql_result = GenerateSqlSuccess(
        sql=(
            "SELECT COUNT(*) AS shipped_count FROM warehouse_order "
            "WHERE shipped_at >= '2026-05-29' AND status = 'shipped'"
        ),
        warnings=[],
        tables_used=["warehouse_order"],
        matched_groups=["inventory_orders"],
        attempt_count=1,
        react_trace=None,
    )
    monkeypatch.setattr(main, "generate_sql", AsyncMock(return_value=sql_result))
    monkeypatch.setattr(
        mysql_executor,
        "execute_sql",
        AsyncMock(return_value=(["shipped_count"], [(3,)], [])),
    )
    monkeypatch.setattr(
        answer_generator,
        "generate_answer",
        AsyncMock(return_value=("3 orders were shipped last week.", [])),
    )

    response = await client.post(
        "/ask",
        json={"query": "how many orders were shipped last week", "top_k": 5},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] in {"ok", "clarification_needed"}
    _assert_no_forbidden_terms(
        body.get("sql") or "",
        PRODUCTION_TABLE_TERMS,
        "POST /ask",
    )
    _assert_no_forbidden_terms(
        json.dumps(body.get("warnings") or []),
        PRODUCTION_TABLE_TERMS,
        "POST /ask warnings",
    )

    stream_response = await client.post(
        "/ask/stream",
        json={"query": "how many orders were shipped last week", "top_k": 5},
    )
    stream_events = [json.loads(line) for line in stream_response.text.splitlines()]
    visible_events = [event for event in stream_events if event["event"] != "trace"]

    assert stream_response.status_code == 200
    assert stream_response.headers["content-type"].startswith("application/x-ndjson")
    assert visible_events[0]["event"] == "started"
    assert visible_events[-1]["event"] == "final"
    assert isinstance(visible_events[-1]["response"], dict)
    _assert_no_forbidden_terms(
        json.dumps(visible_events[-1]["response"], sort_keys=True),
        PRODUCTION_TABLE_TERMS,
        "POST /ask/stream final response",
    )


def test_governance_rules_are_schema_neutral(
    monkeypatch: pytest.MonkeyPatch,
):
    from nl2sql_service.config import settings

    monkeypatch.setattr(settings, "governance_enabled", True)
    monkeypatch.setattr(settings, "governance_enabled_rules", "all")
    rulebook._config = None
    active_rules = rulebook.get_active_rules(rulebook.get_config(settings))

    assert active_rules, "Expected active governance rules from nl2sql_service.rulebook."
    for rule in active_rules:
        serialized = "\n".join(
            [
                rule.name,
                rule.description,
                rule.instruction,
            ]
        )
        _assert_no_forbidden_terms(
            serialized,
            GOVERNANCE_FORBIDDEN_TERMS,
            f"rulebook rule '{rule.name}'",
        )
