from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp_server.client import NL2SQLClient
from mcp_server.config import MCPSettings, load_settings
from mcp_server.errors import AuthError, MCPError
from mcp_server.security.rate_limiter import RedisRateLimiter
from mcp_server.security.tenant import TenantContext, extract_tenant_context
from mcp_server.tools.execute import run as execute_query_run
from mcp_server.tools.explain import run as explain_query_run
from mcp_server.tools.export import run as export_csv_run
from mcp_server.tools.generate import run as generate_sql_run
from mcp_server.tools.rules import run as get_business_rules_run
from mcp_server.tools.schema import run as get_schema_run
from mcp_server.tools.validate import run as validate_sql_run

logger = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - exercised only when MCP SDK is missing.
    FastMCP = None  # type: ignore[assignment]


VERSION = "0.1.0"
ToolHandler = Callable[[dict[str, Any], TenantContext, NL2SQLClient, MCPSettings], Awaitable[dict[str, Any]]]


def _extract_bearer_token(args: dict[str, Any]) -> str:
    auth = str(args.pop("authorization", "") or args.pop("Authorization", ""))
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    if auth:
        return auth
    raise AuthError("Missing Authorization bearer token")


def _build_dependencies(args: dict[str, Any], app_settings: MCPSettings) -> tuple[NL2SQLClient, TenantContext]:
    token = _extract_bearer_token(args)
    tenant_context = extract_tenant_context(token, app_settings)
    client = NL2SQLClient(settings=app_settings)
    return client, tenant_context


async def _check_rate_limit(
    limiter: RedisRateLimiter,
    ctx: TenantContext,
    tool_name: str,
    settings: MCPSettings,
) -> None:
    if not settings.rate_limit_enabled:
        return
    limit = settings.rate_limit_requests_per_minute
    if tool_name == "execute_query":
        limit = max(limit // 2, 1)
    result = await limiter.check(ctx.tenant_id, tool_name, limit)
    if not result.allowed:
        raise MCPError(
            "rate_limited",
            "Rate limit exceeded",
            {"remaining": result.remaining, "reset_at": result.reset_at.isoformat()},
        )


async def _run_tool(
    handler: ToolHandler,
    tool_name: str,
    args: dict[str, Any] | None,
    *,
    app_settings: MCPSettings,
    limiter: RedisRateLimiter,
) -> dict[str, Any]:
    payload = dict(args or {})
    try:
        client, ctx = _build_dependencies(payload, app_settings)
        await _check_rate_limit(limiter, ctx, tool_name, app_settings)
        return await handler(payload, ctx, client, app_settings)
    except MCPError as exc:
        return {"error": exc.to_dict()}
    except Exception as exc:
        logger.exception("MCP tool failed")
        return {"error": {"code": "internal_error", "message": str(exc)}}


async def health_check(app_settings: MCPSettings, limiter: RedisRateLimiter | None = None) -> dict[str, Any]:
    client = NL2SQLClient(settings=app_settings)
    nl2sql_status: Any
    redis_status = "ok"
    try:
        nl2sql_status = await client.health()
    except Exception as exc:
        nl2sql_status = {"status": "down", "error": str(exc)}
    try:
        active_limiter = limiter or RedisRateLimiter(app_settings.redis_url)
        await active_limiter.redis.ping()
    except Exception as exc:
        redis_status = f"down: {exc}"
    degraded = redis_status != "ok" or (
        isinstance(nl2sql_status, dict) and nl2sql_status.get("status") not in {"ok", "healthy"}
    )
    return {
        "status": "degraded" if degraded else "ok",
        "nl2sql_status": nl2sql_status,
        "redis_status": redis_status,
        "version": VERSION,
    }


def create_server(app_settings: MCPSettings) -> Any:
    if FastMCP is None:
        raise RuntimeError("The mcp Python SDK is not installed. Install the MCP requirements first.")
    server = FastMCP(
        "nl2sql-mcp",
        host=app_settings.mcp_server_host,
        port=app_settings.mcp_server_port,
    )
    limiter = RedisRateLimiter(app_settings.redis_url)

    @server.tool()
    async def get_schema(arguments: dict[str, Any]) -> dict[str, Any]:
        return await _run_tool(get_schema_run, "get_schema", arguments, app_settings=app_settings, limiter=limiter)

    @server.tool()
    async def get_business_rules(arguments: dict[str, Any]) -> dict[str, Any]:
        return await _run_tool(
            get_business_rules_run,
            "get_business_rules",
            arguments,
            app_settings=app_settings,
            limiter=limiter,
        )

    @server.tool()
    async def generate_sql(arguments: dict[str, Any]) -> dict[str, Any]:
        return await _run_tool(generate_sql_run, "generate_sql", arguments, app_settings=app_settings, limiter=limiter)

    @server.tool()
    async def validate_sql(arguments: dict[str, Any]) -> dict[str, Any]:
        return await _run_tool(validate_sql_run, "validate_sql", arguments, app_settings=app_settings, limiter=limiter)

    @server.tool()
    async def execute_query(arguments: dict[str, Any]) -> dict[str, Any]:
        return await _run_tool(execute_query_run, "execute_query", arguments, app_settings=app_settings, limiter=limiter)

    @server.tool()
    async def explain_query(arguments: dict[str, Any]) -> dict[str, Any]:
        return await _run_tool(explain_query_run, "explain_query", arguments, app_settings=app_settings, limiter=limiter)

    @server.tool()
    async def export_csv(arguments: dict[str, Any]) -> dict[str, Any]:
        return await _run_tool(export_csv_run, "export_csv", arguments, app_settings=app_settings, limiter=limiter)

    if hasattr(server, "custom_route"):

        @server.custom_route("/health", methods=["GET"])
        async def health(_request: Request) -> Response:
            return JSONResponse(await health_check(app_settings, limiter=limiter))

    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = create_server(load_settings())
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
