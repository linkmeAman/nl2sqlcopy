from __future__ import annotations

import httpx
import pytest

from nl2sql_service import help_docs


def _ok_html(resp: httpx.Response) -> str:
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert "text/html" in resp.headers["content-type"]
    return resp.text


@pytest.mark.asyncio
async def test_help_index_lists_current_routes_and_search(client: httpx.AsyncClient, app) -> None:
    resp = await client.get("/help")
    html = _ok_html(resp)

    assert 'id="route-search"' in html
    assert "Filter by path, method, module, or description" in html

    index = help_docs.build_help_index(app.openapi())
    for endpoint in index.endpoints:
        assert endpoint.method in html
        assert endpoint.path in html


@pytest.mark.asyncio
async def test_help_module_page_filters_to_module(client: httpx.AsyncClient) -> None:
    resp = await client.get("/help/generation")
    html = _ok_html(resp)

    assert "Generation Routes" in html
    assert "/ask" in html
    assert "/generate-sql" in html
    assert "/ingest/knowledge" not in html


@pytest.mark.asyncio
async def test_help_detail_page_includes_route_contract(client: httpx.AsyncClient) -> None:
    resp = await client.get("/help/generation/ask")
    html = _ok_html(resp)

    assert "Ask Question" in html
    assert "POST" in html
    assert "/ask" in html
    assert "Request Body" in html
    assert "Expected Return Format" in html
    assert "How To Call" in html
    assert "curl -s -X POST" in html
    assert "Error Responses" in html
    assert "Authentication" in html
    assert "Related Routes" in html


@pytest.mark.asyncio
async def test_help_routes_are_db_free(app) -> None:
    original = getattr(app.state, "pool", None)
    app.state.pool = None
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/help")
        html = _ok_html(resp)
        assert "NL2SQL Route Help" in html
    finally:
        app.state.pool = original


def test_help_docs_cover_non_internal_openapi_routes(app) -> None:
    openapi_schema = app.openapi()
    index = help_docs.build_help_index(openapi_schema)
    route_keys = {
        help_docs.route_key(method, path)
        for path, method, _operation in help_docs.iter_openapi_operations(openapi_schema)
    }

    assert route_keys == set(index.by_key)
    assert all(endpoint.title and endpoint.summary for endpoint in index.endpoints)
