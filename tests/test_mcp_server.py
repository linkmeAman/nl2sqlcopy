from __future__ import annotations

from datetime import datetime
from typing import Any

import jwt
import pytest

from mcp_server.config import MCPSettings
from mcp_server.errors import AuthError
from mcp_server.security.rate_limiter import RedisRateLimiter
from mcp_server.security.tenant import TenantContext, enforce_tenant_filter, extract_tenant_context
from mcp_server.tools.execute import run as execute_query
from mcp_server.tools.explain import run as explain_query
from mcp_server.tools.export import run as export_csv
from mcp_server.tools.generate import run as generate_sql
from mcp_server.tools.schema import run as get_schema
from mcp_server.tools.validate import structurally_validate_sql


class MockClient:
    def __init__(self) -> None:
        self.executed = False
        self.validated = False

    async def get_schema_groups(self, group_names: list[str], tenant_id: str | None = None) -> dict[str, Any]:
        del tenant_id
        return {"groups": group_names, "tables_in_scope": ["inquiry", "secret_table"]}

    async def generate_sql(self, query: str, top_k: int, tenant_id: str) -> dict[str, Any]:
        del query, top_k, tenant_id
        return {
            "status": "ok",
            "sql": "SELECT id, password_hash, email FROM users",
            "tables_used": ["users"],
            "warnings": [],
            "cache_hit": False,
        }

    async def validate_sql(
        self,
        sql: str,
        query: str,
        tables_in_scope: list[str],
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        del sql, query, tables_in_scope, tenant_id
        self.validated = True
        return {"passes": True, "violations": []}

    async def ask(self, query: str, top_k: int, request_id: str, tenant_id: str) -> dict[str, Any]:
        del query, top_k, request_id, tenant_id
        self.executed = True
        return {
            "rows": [{"id": 1, "email": "a@example.com", "password_hash": "x"}],
            "columns": ["id", "email", "password_hash"],
            "answer": "one row",
        }


class BlockingClient(MockClient):
    async def validate_sql(
        self,
        sql: str,
        query: str,
        tables_in_scope: list[str],
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        del sql, query, tables_in_scope, tenant_id
        self.validated = True
        return {"passes": False, "violations": ["blocked"]}


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.members: dict[str, dict[str, float]] = {}

    async def zremrangebyscore(self, key: str, minimum: float, maximum: float) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        bucket = self.members.setdefault(key, {})
        for member, score in list(bucket.items()):
            if minimum <= score <= maximum:
                del bucket[member]

    async def zcount(self, key: str, minimum: float, maximum: float) -> int:
        if self.fail:
            raise RuntimeError("redis down")
        return sum(1 for score in self.members.setdefault(key, {}).values() if minimum <= score <= maximum)

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.members.setdefault(key, {}).update(mapping)

    async def expire(self, key: str, window_seconds: int) -> None:
        del key, window_seconds


@pytest.fixture
def mcp_settings(tmp_path) -> MCPSettings:
    acl = tmp_path / "column_acl.json"
    acl.write_text(
        '{"blocked_columns": ["password_hash", "salary", "api_key"], '
        '"role_overrides": {"admin": {"allowed_extra": ["salary"]}, "readonly": {"blocked_extra": ["phone"]}}}',
        encoding="utf-8",
    )
    return MCPSettings(
        jwt_secret="super-secret-key-with-enough-bytes",
        column_acl_config_path=str(acl),
        rate_limit_enabled=True,
        max_rows_per_query=10,
    )


@pytest.fixture
def tenant() -> TenantContext:
    return TenantContext(tenant_id="tenant_1", user_id="user_1", role="readonly", allowed_tables=["inquiry"])


@pytest.mark.asyncio
async def test_get_schema_returns_groups_from_nl2sql_service(mcp_settings: MCPSettings, tenant: TenantContext) -> None:
    result = await get_schema(
        {"groups": ["inquiry_lifecycle"]},
        tenant,
        MockClient(),
        mcp_settings,
    )
    assert result["groups"] == ["inquiry_lifecycle"]
    assert result["tables_in_scope"] == ["inquiry"]


@pytest.mark.asyncio
async def test_generate_sql_applies_column_acl_correctly(mcp_settings: MCPSettings, tenant: TenantContext) -> None:
    result = await generate_sql(
        {"query": "show users", "top_k": 5},
        tenant,
        MockClient(),
        mcp_settings,
    )
    assert "password_hash" not in result["sql"]
    assert "email" in result["sql"]


def test_validate_sql_blocks_mutating_statements(mcp_settings: MCPSettings) -> None:
    for sql in ["INSERT INTO x VALUES (1)", "UPDATE x SET id=1", "DELETE FROM x", "DROP TABLE x"]:
        passes, violations, safe_sql = structurally_validate_sql(sql, mcp_settings)
        assert not passes
        assert violations
        assert safe_sql is None


def test_validate_sql_injects_limit_when_missing(mcp_settings: MCPSettings) -> None:
    passes, violations, safe_sql = structurally_validate_sql("SELECT id FROM users", mcp_settings)
    assert passes
    assert not violations
    assert "LIMIT 10" in safe_sql


def test_validate_sql_caps_limit_at_max_rows_per_query(mcp_settings: MCPSettings) -> None:
    passes, _, safe_sql = structurally_validate_sql("SELECT id FROM users LIMIT 100", mcp_settings)
    assert passes
    assert "LIMIT 10" in safe_sql


@pytest.mark.asyncio
async def test_execute_query_calls_validate_first_and_does_not_execute_on_failure(
    mcp_settings: MCPSettings,
    tenant: TenantContext,
) -> None:
    client = BlockingClient()
    result = await execute_query(
        {"sql": "SELECT id FROM users", "query": "show users", "tables_in_scope": ["users"]},
        tenant,
        client,
        mcp_settings,
    )
    assert result["error"] == "validation_failed"
    assert client.validated
    assert not client.executed


def test_tenant_isolation_injects_where_tenant_id_via_sqlglot(mcp_settings: MCPSettings, tenant: TenantContext) -> None:
    sql = enforce_tenant_filter("SELECT id FROM users", tenant, mcp_settings)
    assert "WHERE tenant_id = 'tenant_1'" in sql


@pytest.mark.asyncio
async def test_rate_limiter_blocks_after_limit_exceeded() -> None:
    limiter = RedisRateLimiter("redis://example", client=FakeRedis())
    first = await limiter.check("tenant_1", "generate_sql", 1)
    second = await limiter.check("tenant_1", "generate_sql", 1)
    assert first.allowed
    assert not second.allowed


@pytest.mark.asyncio
async def test_rate_limiter_fails_open_when_redis_is_unavailable() -> None:
    limiter = RedisRateLimiter("redis://example", client=FakeRedis(fail=True))
    result = await limiter.check("tenant_1", "generate_sql", 1)
    assert result.allowed
    assert result.remaining == 1
    assert isinstance(result.reset_at, datetime)


@pytest.mark.asyncio
async def test_explain_query_returns_table_and_column_extraction(
    mcp_settings: MCPSettings,
    tenant: TenantContext,
) -> None:
    result = await explain_query(
        {"sql": "SELECT u.id, u.email FROM users u WHERE u.id = 1"},
        tenant,
        MockClient(),
        mcp_settings,
    )
    assert result["tables"] == ["users"]
    assert result["columns"] == ["email", "id"]
    assert result["estimated_complexity"] == "simple"


@pytest.mark.asyncio
async def test_export_csv_caps_at_max_rows_per_query(
    mcp_settings: MCPSettings,
    tenant: TenantContext,
) -> None:
    result = await export_csv(
        {"rows": [{"id": i} for i in range(20)], "columns": ["id"], "filename": "x.csv"},
        tenant,
        MockClient(),
        mcp_settings,
    )
    assert result["row_count"] == 10
    assert result["csv_content"].count("\n") == 11


def test_jwt_extraction_raises_auth_error_on_invalid_token(mcp_settings: MCPSettings) -> None:
    with pytest.raises(AuthError):
        extract_tenant_context("not-a-token", mcp_settings)
    valid = jwt.encode(
        {"tenant_id": "tenant_1", "sub": "user_1"},
        "super-secret-key-with-enough-bytes",
        algorithm="HS256",
    )
    assert extract_tenant_context(valid, mcp_settings).tenant_id == "tenant_1"
