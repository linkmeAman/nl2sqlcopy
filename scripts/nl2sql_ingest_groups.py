"""
nl2sql_ingest_groups.py
=======================
Trigger schema-group ingestion via HTTP only.

Usage
-----
python scripts/nl2sql_ingest_groups.py --url http://localhost:8080
python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --groups inquiry_lifecycle billing
python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --knowledge
python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --knowledge --column-limit 300 --sql-example-limit 100
python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --all
python scripts/nl2sql_ingest_groups.py --url http://localhost:8080 --all --column-limit 300 --sql-example-limit 200
"""

from __future__ import annotations

import argparse
import json
import time
import sys
import urllib.error
import urllib.request


def _post(url: str, payload: dict, timeout: int = 300) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def _get(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def _wait_until_ready(base_url: str, *, timeout: int, poll_interval: float = 1.0) -> None:
    deadline = time.monotonic() + max(1, timeout)
    health_url = base_url.rstrip("/") + "/health/runtime"
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            payload = _get(health_url, timeout=min(timeout, 10))
            if str(payload.get("status", "")).lower() == "ok":
                return
            last_error = f"runtime health status={payload.get('status')!r}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(poll_interval)
    raise RuntimeError(f"Service did not become ready at {health_url}: {last_error or 'unknown error'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest rag_schema groups via /ingest/groups")
    parser.add_argument("--url", default="http://localhost:8080", help="Base URL of the service")
    parser.add_argument(
        "--groups",
        nargs="*",
        default=None,
        help="Optional subset of entity_id names. Omit for all groups.",
    )
    parser.add_argument(
        "--knowledge",
        action="store_true",
        help="Use /ingest/knowledge to embed column catalog and SQL examples.",
    )
    parser.add_argument(
        "--column-limit",
        type=int,
        default=None,
        help="Optional max number of column-catalog chunks to ingest.",
    )
    parser.add_argument(
        "--sql-example-limit",
        type=int,
        default=200,
        help="Optional max number of SQL-example chunks to ingest.",
    )
    parser.add_argument(
        "--skip-columns",
        action="store_true",
        help="Skip column-catalog chunks when --knowledge is used.",
    )
    parser.add_argument(
        "--skip-sql",
        action="store_true",
        help="Skip SQL-example chunks when --knowledge is used.",
    )
    parser.add_argument(
        "--skip-relations",
        action="store_true",
        help="Skip relation-link chunks when --knowledge is used.",
    )
    parser.add_argument(
        "--skip-graph",
        action="store_true",
        help="Skip table-node chunks from table_graph.json when --knowledge is used.",
    )
    parser.add_argument(
        "--skip-view-registry",
        action="store_true",
        help="Skip view-node chunks from view_registry.json when --knowledge is used.",
    )
    parser.add_argument(
        "--skip-rules",
        action="store_true",
        help="Skip onboarding-rules chunk when --knowledge is used.",
    )
    parser.add_argument(
        "--relation-limit",
        type=int,
        default=None,
        help="Optional max number of relation-link chunks to ingest.",
    )
    parser.add_argument(
        "--graph-limit",
        type=int,
        default=None,
        help="Optional max number of table-node chunks to ingest.",
    )
    parser.add_argument(
        "--view-registry-limit",
        type=int,
        default=None,
        help="Optional max number of view-node chunks to ingest.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run full serial ingest: groups first, then all knowledge sources.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="HTTP request timeout in seconds (default: 300). Increase for large knowledge ingests.",
    )
    parser.add_argument(
        "--ready-timeout",
        type=int,
        default=90,
        help="How long to wait for /health/runtime to report ok before posting ingest requests.",
    )
    args = parser.parse_args()

    base = args.url.rstrip("/")
    try:
        _wait_until_ready(base, timeout=args.ready_timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.all:
        _run_step(
            base + "/ingest/groups",
            {},
            "1/2 — schema groups",
            timeout=args.timeout,
        )
        _run_step(
            base + "/ingest/knowledge",
            {
                "include_column_catalog": not args.skip_columns,
                "include_sql_examples": not args.skip_sql,
                "include_relations": not args.skip_relations,
                "include_graph": not args.skip_graph,
                "include_view_registry": not args.skip_view_registry,
                "include_onboarding_rules": not args.skip_rules,
                "column_limit": args.column_limit,
                "sql_example_limit": args.sql_example_limit,
                "relation_limit": args.relation_limit,
                "graph_limit": args.graph_limit,
                "view_registry_limit": args.view_registry_limit,
            },
            "2/2 — knowledge (columns + SQL examples + relations + graph + view registry + rules)",
            timeout=args.timeout,
        )
        return

    payload: dict
    if args.knowledge:
        endpoint = base + "/ingest/knowledge"
        payload = {
            "include_column_catalog": not args.skip_columns,
            "include_sql_examples": not args.skip_sql,
            "include_relations": not args.skip_relations,
            "include_graph": not args.skip_graph,
            "include_view_registry": not args.skip_view_registry,
            "include_onboarding_rules": not args.skip_rules,
            "column_limit": args.column_limit,
            "sql_example_limit": args.sql_example_limit,
            "relation_limit": args.relation_limit,
            "graph_limit": args.graph_limit,
            "view_registry_limit": args.view_registry_limit,
        }
    else:
        endpoint = base + "/ingest/groups"
        if args.groups is None or len(args.groups) == 0:
            payload = {}
        else:
            payload = {"group_names": args.groups}

    _run_step(endpoint, payload, timeout=args.timeout)


def _run_step(endpoint: str, payload: dict, label: str = "", timeout: int = 300) -> None:
    if label:
        print(f"\n=== {label} ===")
    print(f"POST {endpoint}")
    print(f"Payload: {json.dumps(payload)}")
    try:
        result = _post(endpoint, payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    print(
        "Done. inserted={inserted} updated={updated} source={source}".format(
            inserted=result.get("inserted", 0),
            updated=result.get("updated", 0),
            source=result.get("source", ""),
        )
    )


if __name__ == "__main__":
    main()
