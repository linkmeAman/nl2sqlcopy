from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from nl2sql_service.key_vault import encrypt_api_key
from nl2sql_service.models import (
    ApiKeyRecord,
    CreateProviderRequest,
    ModelRecord,
    ProviderConfig,
    RegisterModelRequest,
    UpdateModelRequest,
    UpdateProviderRequest,
)


def _normalize_provider_name(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _provider_from_row(row: asyncpg.Record | dict[str, Any]) -> ProviderConfig:
    data = dict(row)
    return ProviderConfig(
        id=data["id"],
        provider_name=str(data["provider_name"]),
        display_name=str(data["display_name"]),
        base_url=data.get("base_url"),
        org_id=data.get("org_id"),
        extra_config=dict(data.get("extra_config") or {}),
        is_active=bool(data["is_active"]),
        is_local=bool(data["is_local"]),
        key_count=int(data.get("key_count") or 0),
        model_count=int(data.get("model_count") or 0),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


def _api_key_from_row(row: asyncpg.Record | dict[str, Any]) -> ApiKeyRecord:
    data = dict(row)
    api_key_hash = str(data.get("api_key_hash") or "")
    return ApiKeyRecord(
        id=data["id"],
        provider_id=data["provider_id"],
        key_label=str(data["key_label"]),
        key_prefix=api_key_hash[:8],
        is_active=bool(data["is_active"]),
        created_at=data["created_at"],
    )


def _model_from_row(row: asyncpg.Record | dict[str, Any]) -> ModelRecord:
    data = dict(row)
    return ModelRecord(
        id=data["id"],
        provider_id=data["provider_id"],
        provider_name=str(data["provider_name"]),
        model_name=str(data["model_name"]),
        display_name=data.get("display_name"),
        role=str(data["role"]),
        is_default=bool(data["is_default"]),
        is_active=bool(data["is_active"]),
        supports_tools=bool(data["supports_tools"]),
        supports_stream=bool(data.get("supports_stream", True)),
        context_window=data.get("context_window"),
        api_key_id=data.get("api_key_id"),
        extra_config=dict(data.get("extra_config") or {}),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


_PROVIDER_SELECT = """
SELECT
    p.*,
    COALESCE((
        SELECT COUNT(*)::int
        FROM nl2sql_llm_api_keys k
        WHERE k.provider_id = p.id AND k.is_active = true
    ), 0) AS key_count,
    COALESCE((
        SELECT COUNT(*)::int
        FROM nl2sql_model_registry m
        WHERE m.provider_id = p.id AND m.is_active = true
    ), 0) AS model_count
FROM nl2sql_llm_providers p
"""

_MODEL_SELECT = """
SELECT
    m.*,
    p.provider_name
FROM nl2sql_model_registry m
JOIN nl2sql_llm_providers p ON p.id = m.provider_id
"""


async def list_providers(pool: asyncpg.Pool) -> list[ProviderConfig]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_PROVIDER_SELECT + " ORDER BY p.display_name, p.provider_name")
    return [_provider_from_row(row) for row in rows]


async def get_provider(pool: asyncpg.Pool, provider_id: UUID) -> ProviderConfig | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_PROVIDER_SELECT + " WHERE p.id = $1", provider_id)
    return _provider_from_row(row) if row else None


async def create_provider(pool: asyncpg.Pool, data: CreateProviderRequest) -> ProviderConfig:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO nl2sql_llm_providers (
                provider_name,
                display_name,
                base_url,
                org_id,
                extra_config,
                is_active,
                is_local
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, true, $6)
            RETURNING id
            """,
            _normalize_provider_name(data.provider_name),
            data.display_name.strip(),
            data.base_url,
            data.org_id,
            data.extra_config,
            data.is_local,
        )
    if row is None:
        raise RuntimeError("Provider insert did not return an id.")
    provider = await get_provider(pool, row["id"])
    if provider is None:
        raise RuntimeError("Provider insert could not be reloaded.")
    return provider


async def update_provider(
    pool: asyncpg.Pool,
    provider_id: UUID,
    data: UpdateProviderRequest,
) -> ProviderConfig:
    updates = data.model_dump(exclude_unset=True)
    if not updates:
        provider = await get_provider(pool, provider_id)
        if provider is None:
            raise ValueError("Provider not found.")
        return provider

    assignments: list[str] = []
    values: list[Any] = [provider_id]
    field_map = {
        "display_name": "display_name",
        "base_url": "base_url",
        "org_id": "org_id",
        "is_active": "is_active",
        "is_local": "is_local",
        "extra_config": "extra_config",
    }
    index = 2
    for key, column in field_map.items():
        if key not in updates:
            continue
        value = updates[key]
        cast = "::jsonb" if key == "extra_config" else ""
        assignments.append(f"{column} = ${index}{cast}")
        values.append(
            _normalize_provider_name(str(value)) if key == "provider_name" else value
        )
        index += 1
    assignments.append("updated_at = now()")
    if not assignments:
        provider = await get_provider(pool, provider_id)
        if provider is None:
            raise ValueError("Provider not found.")
        return provider

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE nl2sql_llm_providers
            SET {", ".join(assignments)}
            WHERE id = $1
            RETURNING id
            """,
            *values,
        )
    if row is None:
        raise ValueError("Provider not found.")
    provider = await get_provider(pool, row["id"])
    if provider is None:
        raise RuntimeError("Provider update could not be reloaded.")
    return provider


async def deactivate_provider(pool: asyncpg.Pool, provider_id: UUID) -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            updated = await conn.fetchrow(
                """
                UPDATE nl2sql_llm_providers
                SET is_active = false, updated_at = now()
                WHERE id = $1
                RETURNING id
                """,
                provider_id,
            )
            if updated is None:
                return False
            await conn.execute(
                """
                UPDATE nl2sql_model_registry
                SET is_active = false, is_default = false, updated_at = now()
                WHERE provider_id = $1
                """,
                provider_id,
            )
            await conn.execute(
                """
                UPDATE nl2sql_llm_api_keys
                SET is_active = false
                WHERE provider_id = $1
                """,
                provider_id,
            )
    return True


async def add_api_key(
    pool: asyncpg.Pool,
    provider_id: UUID,
    label: str,
    raw_key: str,
) -> ApiKeyRecord:
    key_hash, key_enc = encrypt_api_key(raw_key)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO nl2sql_llm_api_keys (
                provider_id,
                key_label,
                api_key_hash,
                api_key_enc,
                is_active
            )
            VALUES ($1, $2, $3, $4, true)
            RETURNING *
            """,
            provider_id,
            label.strip(),
            key_hash,
            key_enc,
        )
    if row is None:
        raise RuntimeError("Provider key insert failed.")
    return _api_key_from_row(row)


async def list_api_keys(pool: asyncpg.Pool, provider_id: UUID) -> list[ApiKeyRecord]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
            FROM nl2sql_llm_api_keys
            WHERE provider_id = $1
            ORDER BY created_at DESC
            """,
            provider_id,
        )
    return [_api_key_from_row(row) for row in rows]


async def deactivate_api_key(pool: asyncpg.Pool, key_id: UUID) -> bool:
    async with pool.acquire() as conn:
        status = await conn.execute(
            """
            UPDATE nl2sql_llm_api_keys
            SET is_active = false
            WHERE id = $1
            """,
            key_id,
        )
    return not status.endswith("0")


async def list_models(
    pool: asyncpg.Pool,
    role: str | None = None,
    active_only: bool = True,
) -> list[ModelRecord]:
    clauses: list[str] = []
    values: list[Any] = []
    if role is not None:
        values.append(role)
        clauses.append(f"m.role = ${len(values)}")
    if active_only:
        clauses.append("m.is_active = true")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _MODEL_SELECT + where + " ORDER BY m.role, p.provider_name, m.model_name",
            *values,
        )
    return [_model_from_row(row) for row in rows]


async def register_model(pool: asyncpg.Pool, data: RegisterModelRequest) -> ModelRecord:
    async with pool.acquire() as conn:
        async with conn.transaction():
            if data.is_default:
                await conn.execute(
                    """
                    UPDATE nl2sql_model_registry
                    SET is_default = false, updated_at = now()
                    WHERE role = $1 AND is_active = true
                    """,
                    data.role,
                )
            row = await conn.fetchrow(
                """
                INSERT INTO nl2sql_model_registry (
                    provider_id,
                    api_key_id,
                    model_name,
                    display_name,
                    role,
                    context_window,
                    supports_tools,
                    supports_stream,
                    is_default,
                    is_active,
                    extra_config
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, true, $10::jsonb)
                RETURNING id
                """,
                data.provider_id,
                data.api_key_id,
                data.model_name.strip(),
                data.display_name,
                data.role.strip(),
                data.context_window,
                data.supports_tools,
                data.supports_stream,
                data.is_default,
                data.extra_config,
            )
    if row is None:
        raise RuntimeError("Model insert failed.")
    model = await get_model_by_id(pool, row["id"])
    if model is None:
        raise RuntimeError("Model insert could not be reloaded.")
    return model


async def get_model_by_id(pool: asyncpg.Pool, model_id: UUID) -> ModelRecord | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_MODEL_SELECT + " WHERE m.id = $1", model_id)
    return _model_from_row(row) if row else None


async def update_model(
    pool: asyncpg.Pool,
    model_id: UUID,
    data: UpdateModelRequest,
) -> ModelRecord:
    updates = data.model_dump(exclude_unset=True)
    existing = await get_model_by_id(pool, model_id)
    if existing is None:
        raise ValueError("Model not found.")
    if not updates:
        return existing

    if updates.get("is_default") is True:
        return await set_default_model(pool, model_id, existing.role)

    assignments: list[str] = []
    values: list[Any] = [model_id]
    field_map = {
        "display_name": "display_name",
        "api_key_id": "api_key_id",
        "context_window": "context_window",
        "supports_tools": "supports_tools",
        "supports_stream": "supports_stream",
        "is_active": "is_active",
        "extra_config": "extra_config",
    }
    index = 2
    for key, column in field_map.items():
        if key not in updates:
            continue
        cast = "::jsonb" if key == "extra_config" else ""
        assignments.append(f"{column} = ${index}{cast}")
        values.append(updates[key])
        index += 1
    if updates.get("is_default") is False:
        assignments.append("is_default = false")
    assignments.append("updated_at = now()")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE nl2sql_model_registry
            SET {", ".join(assignments)}
            WHERE id = $1
            RETURNING id
            """,
            *values,
        )
    if row is None:
        raise ValueError("Model not found.")
    model = await get_model_by_id(pool, row["id"])
    if model is None:
        raise RuntimeError("Model update could not be reloaded.")
    return model


async def set_default_model(pool: asyncpg.Pool, model_id: UUID, role: str) -> ModelRecord:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE nl2sql_model_registry
                SET is_default = false, updated_at = now()
                WHERE role = $1 AND is_active = true
                """,
                role,
            )
            row = await conn.fetchrow(
                """
                UPDATE nl2sql_model_registry
                SET is_default = true, is_active = true, updated_at = now()
                WHERE id = $1
                RETURNING id
                """,
                model_id,
            )
    if row is None:
        raise ValueError("Model not found.")
    model = await get_model_by_id(pool, row["id"])
    if model is None:
        raise RuntimeError("Default model update could not be reloaded.")
    return model


async def deactivate_model(pool: asyncpg.Pool, model_id: UUID) -> bool:
    async with pool.acquire() as conn:
        status = await conn.execute(
            """
            UPDATE nl2sql_model_registry
            SET is_active = false, is_default = false, updated_at = now()
            WHERE id = $1
            """,
            model_id,
        )
    return not status.endswith("0")


async def get_default_model(pool: asyncpg.Pool, role: str) -> ModelRecord | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _MODEL_SELECT
            + """
            WHERE m.role = $1
              AND m.is_default = true
              AND m.is_active = true
              AND p.is_active = true
            LIMIT 1
            """,
            role,
        )
    return _model_from_row(row) if row else None


async def get_default_model_config(pool: asyncpg.Pool, role: str) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                m.id,
                m.provider_id,
                m.api_key_id,
                m.model_name,
                m.role,
                m.context_window,
                m.supports_tools,
                m.supports_stream,
                m.extra_config AS model_extra_config,
                p.provider_name,
                p.display_name,
                p.base_url,
                p.org_id,
                p.is_local,
                p.extra_config AS provider_extra_config,
                k.api_key_enc
            FROM nl2sql_model_registry m
            JOIN nl2sql_llm_providers p ON p.id = m.provider_id
            LEFT JOIN nl2sql_llm_api_keys k ON k.id = m.api_key_id AND k.is_active = true
            WHERE m.role = $1
              AND m.is_default = true
              AND m.is_active = true
              AND p.is_active = true
            LIMIT 1
            """,
            role,
        )
    return dict(row) if row else None
