from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from nl2sql_service.models import (
    InstructionType,
    LearningStatus,
    SimilarInstruction,
    TeachRequest,
    TeachResponse,
)

logger = logging.getLogger(__name__)

UTC = timezone.utc
_PENDING_TTL = timedelta(minutes=30)
_MAX_PENDING = 100
_pending_instructions: dict[str, dict] = {}


def build_embedding_source(
    instruction_type: str,
    content: str,
    tables_affected: list[str],
) -> str:
    tables = ", ".join(tables_affected)
    templates = {
        InstructionType.TABLE_RELATIONSHIP.value: (
            "Table relationship: {content}\nTables: {tables}"
        ),
        InstructionType.BUSINESS_RULE.value: (
            "Business rule: {content}\nApplies to: {tables}"
        ),
        InstructionType.QUERY_METHODOLOGY.value: (
            "Query methodology: {content}\nContext tables: {tables}"
        ),
        InstructionType.TERM_MAPPING.value: (
            "Term mapping: {content}\nRelated tables: {tables}"
        ),
        InstructionType.FILTER_RULE.value: (
            "Filter rule: {content}\nTables: {tables}"
        ),
        InstructionType.CORRECTION.value: (
            "Correction: {content}\nTables: {tables}"
        ),
    }
    template = templates.get(instruction_type, "Instruction: {content}\nTables: {tables}")
    return template.format(content=content.strip(), tables=tables)


async def find_similar_instructions(
    content: str,
    tables_affected: list[str],
    pool: asyncpg.Pool,
    limit: int = 3,
) -> list[dict]:
    try:
        needle = f"%{content.strip()[:30]}%" if content.strip() else "%"
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    instruction_type,
                    content,
                    embedding_source,
                    tables_affected,
                    confidence_score,
                    is_verified,
                    is_active,
                    conflict_group,
                    source_query,
                    use_count,
                    success_count,
                    failure_count,
                    last_used_at,
                    created_at,
                    updated_at
                FROM nl2sql_user_instructions
                WHERE is_active = TRUE
                  AND (
                    tables_affected && $1::text[]
                    OR content ILIKE $2
                  )
                ORDER BY confidence_score DESC, use_count DESC
                LIMIT $3
                """,
                tables_affected,
                needle,
                limit,
            )
        return [_instruction_row_to_dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to find similar instructions: %s", exc)
        return []


async def detect_conflict(
    instruction_type: str,
    content: str,
    tables_affected: list[str],
    pool: asyncpg.Pool,
) -> dict | None:
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    instruction_type,
                    content,
                    embedding_source,
                    tables_affected,
                    confidence_score,
                    is_verified,
                    is_active,
                    conflict_group,
                    source_query,
                    use_count,
                    success_count,
                    failure_count,
                    last_used_at,
                    created_at,
                    updated_at
                FROM nl2sql_user_instructions
                WHERE is_active = TRUE
                  AND instruction_type = $1
                  AND (
                    tables_affected && $2::text[]
                    OR (tables_affected = '{}'::text[] AND $2::text[] = '{}'::text[])
                  )
                ORDER BY is_verified DESC, confidence_score DESC, use_count DESC
                """,
                instruction_type,
                tables_affected,
            )

        candidates = [_instruction_row_to_dict(row) for row in rows]
        for candidate in candidates:
            if _is_structural_conflict(
                instruction_type=instruction_type,
                new_content=content,
                new_tables=tables_affected,
                existing=candidate,
            ):
                return candidate
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to detect instruction conflict: %s", exc)
        return None


async def save_instruction(
    instruction_type: str,
    content: str,
    tables_affected: list[str],
    source_query: str | None,
    pool: asyncpg.Pool,
    is_verified: bool = False,
    confidence_score: float = 0.7,
    conflict_group: int | None = None,
) -> int:
    try:
        embedding_source = build_embedding_source(
            instruction_type,
            content,
            tables_affected,
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO nl2sql_user_instructions
                    (instruction_type, content, embedding_source,
                     tables_affected, confidence_score, is_verified,
                     source_query, conflict_group)
                VALUES ($1, $2, $3, $4::text[], $5, $6, $7, $8)
                RETURNING id
                """,
                instruction_type,
                content,
                embedding_source,
                tables_affected,
                confidence_score,
                is_verified,
                source_query,
                conflict_group,
            )
        if row is None:
            return -1
        return int(row["id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to save instruction: %s", exc)
        return -1


async def process_teach_request(
    request: TeachRequest,
    pool: asyncpg.Pool,
) -> TeachResponse:
    try:
        instruction_type = request.instruction_type.value
        instruction = {
            "instruction_type": instruction_type,
            "content": request.content,
            "tables_affected": request.tables_affected,
            "source_query": request.source_query,
        }

        if request.instruction_type == InstructionType.CORRECTION:
            similar = await find_similar_instructions(
                content=request.content,
                tables_affected=request.tables_affected,
                pool=pool,
            )
            if similar:
                token = _store_pending(
                    instruction=instruction,
                    conflicting_id=int(similar[0]["id"]),
                )
                return TeachResponse(
                    learning_status=LearningStatus.CONFLICT_DETECTED,
                    message=(
                        "I found an existing instruction that this correction may "
                        "replace. Confirm whether to save this correction, replace "
                        "the existing instruction, or reject it."
                    ),
                    similar_instructions=_similar_models(similar),
                    requires_confirmation=True,
                    confirmation_token=token,
                )

            new_id = await save_instruction(
                instruction_type=instruction_type,
                content=request.content,
                tables_affected=request.tables_affected,
                source_query=request.source_query,
                pool=pool,
                is_verified=True,
                confidence_score=1.0,
            )
            if new_id < 0:
                return _rejected("I could not save this correction.")
            return TeachResponse(
                learning_status=LearningStatus.SAVED_NEW,
                message="This correction is new to me. I've saved it as verified.",
                instruction_id=new_id,
            )

        conflict = await detect_conflict(
            instruction_type=instruction_type,
            content=request.content,
            tables_affected=request.tables_affected,
            pool=pool,
        )
        if conflict:
            token = _store_pending(
                instruction=instruction,
                conflicting_id=int(conflict["id"]),
            )
            tables = ", ".join(request.tables_affected) or "these tables"
            return TeachResponse(
                learning_status=LearningStatus.CONFLICT_DETECTED,
                message=(
                    f"I found an existing rule about {tables}: "
                    f"'{str(conflict['content'])[:100]}'. "
                    "Do you want to replace it, keep both, or cancel?"
                ),
                similar_instructions=_similar_models([conflict]),
                requires_confirmation=True,
                confirmation_token=token,
            )

        similar = await find_similar_instructions(
            content=request.content,
            tables_affected=request.tables_affected,
            pool=pool,
        )
        new_id = await save_instruction(
            instruction_type=instruction_type,
            content=request.content,
            tables_affected=request.tables_affected,
            source_query=request.source_query,
            pool=pool,
            is_verified=False,
            confidence_score=0.7,
        )
        if new_id < 0:
            return _rejected("I could not save this instruction.")

        if similar:
            return TeachResponse(
                learning_status=LearningStatus.SIMILAR_FOUND,
                message=(
                    "This instruction has been saved. "
                    f"I found {len(similar)} similar rule(s). "
                    "Your new rule will be used alongside them."
                ),
                instruction_id=new_id,
                similar_instructions=_similar_models(similar),
            )

        return TeachResponse(
            learning_status=LearningStatus.SAVED_NEW,
            message=(
                "This instruction is new to me. "
                "I've saved it and will use it in future queries."
            ),
            instruction_id=new_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Teach request failed: %s", exc)
        return _rejected(f"I could not process this instruction: {exc}")


async def process_confirmation(
    token: str,
    action: str,
    pool: asyncpg.Pool,
) -> TeachResponse:
    try:
        pending = _pending_instructions.get(token)
        if not pending or _is_expired(pending):
            _pending_instructions.pop(token, None)
            return _rejected("Confirmation token expired or not found.")

        pending = _pending_instructions.pop(token)
        instruction = pending["instruction"]
        conflicting_id = pending.get("conflicting_id")

        if action == "reject":
            return TeachResponse(
                learning_status=LearningStatus.REJECTED,
                message="Pending instruction discarded.",
            )

        if action == "confirm":
            new_id = await save_instruction(
                instruction_type=instruction["instruction_type"],
                content=instruction["content"],
                tables_affected=instruction["tables_affected"],
                source_query=instruction.get("source_query"),
                pool=pool,
                is_verified=True,
                confidence_score=1.0,
            )
            if new_id < 0:
                return _rejected("I could not save the confirmed instruction.")
            return TeachResponse(
                learning_status=LearningStatus.CONFIRMED,
                message="Instruction confirmed and saved. Existing rule kept active.",
                instruction_id=new_id,
            )

        if action == "replace":
            if conflicting_id is not None:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE nl2sql_user_instructions
                        SET is_active = FALSE,
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        conflicting_id,
                    )
            new_id = await save_instruction(
                instruction_type=instruction["instruction_type"],
                content=instruction["content"],
                tables_affected=instruction["tables_affected"],
                source_query=instruction.get("source_query"),
                pool=pool,
                is_verified=True,
                confidence_score=1.0,
                conflict_group=conflicting_id,
            )
            if new_id < 0:
                return _rejected("I could not save the replacement instruction.")
            return TeachResponse(
                learning_status=LearningStatus.CONFIRMED,
                message="Instruction confirmed and replaced the conflicting rule.",
                instruction_id=new_id,
            )

        return _rejected("Unknown confirmation action.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Instruction confirmation failed: %s", exc)
        return _rejected(f"I could not process this confirmation: {exc}")


async def get_relevant_instructions(
    query: str,
    tables_in_scope: list[str],
    pool: asyncpg.Pool,
    min_confidence: float = 0.5,
    limit: int = 5,
) -> list[dict]:
    del query
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    instruction_type,
                    content,
                    embedding_source,
                    tables_affected,
                    confidence_score,
                    is_verified,
                    is_active,
                    conflict_group,
                    source_query,
                    use_count,
                    success_count,
                    failure_count,
                    last_used_at,
                    created_at,
                    updated_at
                FROM nl2sql_user_instructions
                WHERE is_active = TRUE
                  AND confidence_score >= $2
                  AND (
                    tables_affected && $1::text[]
                    OR tables_affected = '{}'::text[]
                  )
                ORDER BY
                    is_verified DESC,
                    confidence_score DESC,
                    use_count DESC
                LIMIT $3
                """,
                tables_in_scope,
                min_confidence,
                limit,
            )
        return [_instruction_row_to_dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load relevant user instructions: %s", exc)
        return []


async def get_rewrite_term_mapping_hints(
    pool: asyncpg.Pool,
    min_confidence: float = 0.5,
    limit: int = 8,
) -> list[str]:
    """Return active term mappings before table scope is known."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    content,
                    tables_affected
                FROM nl2sql_user_instructions
                WHERE is_active = TRUE
                  AND instruction_type = $1
                  AND confidence_score >= $2
                ORDER BY
                    is_verified DESC,
                    confidence_score DESC,
                    use_count DESC,
                    updated_at DESC
                LIMIT $3
                """,
                InstructionType.TERM_MAPPING.value,
                min_confidence,
                limit,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load query rewrite term mappings: %s", exc)
        return []

    hints: list[str] = []
    for row in rows:
        content = str(row["content"] or "").strip()
        if not content:
            continue
        tables = [
            str(table)
            for table in (row["tables_affected"] or [])
            if str(table).strip()
        ]
        if tables:
            hints.append(f"{content} (tables: {', '.join(tables)})")
        else:
            hints.append(content)
    return hints


def format_instructions_for_prompt(
    instructions: list[dict],
) -> str:
    if not instructions:
        return ""

    grouped: dict[str, list[dict]] = {}
    sorted_instructions = sorted(
        instructions,
        key=lambda item: (
            bool(item.get("is_verified")),
            float(item.get("confidence_score") or 0.0),
        ),
        reverse=True,
    )
    for instruction in sorted_instructions:
        instruction_type = str(instruction.get("instruction_type") or "")
        if instruction_type:
            grouped.setdefault(instruction_type, []).append(instruction)

    sections: list[str] = ["USER-PROVIDED RULES (apply these strictly):"]
    section_titles = [
        (InstructionType.TABLE_RELATIONSHIP.value, "Table Relationships"),
        (InstructionType.BUSINESS_RULE.value, "Business Rules"),
        (InstructionType.TERM_MAPPING.value, "Term Mappings"),
        (InstructionType.FILTER_RULE.value, "Filter Rules"),
        (InstructionType.QUERY_METHODOLOGY.value, "Query Methodology"),
        (InstructionType.CORRECTION.value, "Corrections"),
    ]
    for instruction_type, title in section_titles:
        items = grouped.get(instruction_type, [])
        if not items:
            continue
        lines = [f"{title}:"]
        for instruction in items:
            content = str(instruction.get("content") or "").strip()
            if content:
                lines.append(f"- {content}")
        if len(lines) > 1:
            sections.append("\n".join(lines))

    return "\n\n".join(sections) if len(sections) > 1 else ""


async def record_instruction_outcome(
    tables_used: list[str],
    success: bool,
    pool: asyncpg.Pool,
) -> None:
    if not tables_used:
        return

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, confidence_score, failure_count
                FROM nl2sql_user_instructions
                WHERE is_active = TRUE
                  AND tables_affected && $1::text[]
                """,
                tables_used,
            )
            for row in rows:
                instruction_id = int(row["id"])
                if success:
                    await conn.execute(
                        """
                        UPDATE nl2sql_user_instructions
                        SET use_count = use_count + 1,
                            success_count = success_count + 1,
                            last_used_at = NOW(),
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        instruction_id,
                    )
                    continue

                new_failure_count = int(row["failure_count"] or 0) + 1
                confidence = float(row["confidence_score"] or 0.0)
                should_decay = new_failure_count >= 3 and confidence > 0.4
                new_confidence = max(0.3, confidence - 0.1) if should_decay else confidence
                await conn.execute(
                    """
                    UPDATE nl2sql_user_instructions
                    SET use_count = use_count + 1,
                        failure_count = failure_count + 1,
                        confidence_score = $2,
                        last_used_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    instruction_id,
                    new_confidence,
                )
                if new_confidence < 0.4:
                    logger.warning(
                        "Instruction %s confidence below 0.4. "
                        "Consider reviewing or disabling it.",
                        instruction_id,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record instruction outcome: %s", exc)


def _store_pending(instruction: dict, conflicting_id: int | None) -> str:
    _evict_pending()
    token = secrets.token_hex(8)
    _pending_instructions[token] = {
        "instruction": instruction,
        "conflicting_id": conflicting_id,
        "created_at": datetime.now(UTC),
    }
    return token


def _evict_pending() -> None:
    now = datetime.now(UTC)
    expired = [
        token
        for token, pending in _pending_instructions.items()
        if now - _pending_created_at(pending) > _PENDING_TTL
    ]
    for token in expired:
        _pending_instructions.pop(token, None)

    while len(_pending_instructions) >= _MAX_PENDING:
        oldest = min(
            _pending_instructions,
            key=lambda token: _pending_created_at(_pending_instructions[token]),
        )
        _pending_instructions.pop(oldest, None)


def _is_expired(pending: dict) -> bool:
    return datetime.now(UTC) - _pending_created_at(pending) > _PENDING_TTL


def _pending_created_at(pending: dict) -> datetime:
    created_at = pending.get("created_at")
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            return created_at.replace(tzinfo=UTC)
        return created_at
    return datetime.now(UTC) - (_PENDING_TTL + timedelta(seconds=1))


def _similar_models(rows: list[dict]) -> list[SimilarInstruction]:
    models: list[SimilarInstruction] = []
    for row in rows:
        try:
            models.append(
                SimilarInstruction(
                    id=int(row["id"]),
                    instruction_type=str(row["instruction_type"]),
                    content=str(row["content"]),
                    confidence_score=float(row.get("confidence_score", 0.0)),
                    is_verified=bool(row.get("is_verified", False)),
                    use_count=int(row.get("use_count", 0)),
                )
            )
        except Exception:  # noqa: BLE001
            continue
    return models


def _rejected(message: str) -> TeachResponse:
    return TeachResponse(
        learning_status=LearningStatus.REJECTED,
        message=message,
    )


def _instruction_row_to_dict(row: Any) -> dict:
    data = dict(row)
    if data.get("tables_affected") is None:
        data["tables_affected"] = []
    elif not isinstance(data["tables_affected"], list):
        data["tables_affected"] = list(data["tables_affected"])
    return data


def _is_structural_conflict(
    instruction_type: str,
    new_content: str,
    new_tables: list[str],
    existing: dict,
) -> bool:
    existing_content = str(existing.get("content") or "")
    existing_tables = list(existing.get("tables_affected") or [])
    if not _tables_overlap(new_tables, existing_tables):
        return False

    if instruction_type == InstructionType.TERM_MAPPING.value:
        return _mapping_source_term(new_content) == _mapping_source_term(existing_content)

    if instruction_type == InstructionType.FILTER_RULE.value:
        new_column = _filter_column(new_content)
        existing_column = _filter_column(existing_content)
        return bool(new_column and existing_column and new_column == existing_column)

    if instruction_type == InstructionType.TABLE_RELATIONSHIP.value:
        new_pair = _relationship_tables(new_content, new_tables)
        existing_pair = _relationship_tables(existing_content, existing_tables)
        return bool(new_pair and existing_pair and new_pair == existing_pair)

    return True


def _tables_overlap(left: list[str], right: list[str]) -> bool:
    left_set = {_normalize_token(item) for item in left if item}
    right_set = {_normalize_token(item) for item in right if item}
    if not left_set and not right_set:
        return True
    if not left_set or not right_set:
        return False
    return bool(left_set.intersection(right_set))


def _mapping_source_term(content: str) -> str:
    match = re.match(r"\s*(.+?)\s+(?:means|is|=)\s+", content, flags=re.IGNORECASE)
    if match:
        return _normalize_phrase(match.group(1))
    return _normalize_phrase(content.split()[0] if content.split() else "")


def _filter_column(content: str) -> str:
    match = re.search(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\b\s*(?:=|!=|<>|>=|<=|>|<|\bIS\b|\bIN\b|\bLIKE\b)",
        content,
        flags=re.IGNORECASE,
    )
    return _normalize_token(match.group(1)) if match else ""


def _relationship_tables(content: str, tables: list[str]) -> frozenset[str]:
    refs = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.", content)
    normalized_refs = []
    for ref in refs:
        normalized = _normalize_token(ref)
        if normalized and normalized not in normalized_refs:
            normalized_refs.append(normalized)
    if len(normalized_refs) >= 2:
        return frozenset(normalized_refs[:2])
    normalized_tables = [_normalize_token(table) for table in tables if table]
    return frozenset(normalized_tables[:2]) if len(normalized_tables) >= 2 else frozenset()


def _normalize_token(value: str) -> str:
    return value.strip().strip("`\"[]").lower()


def _normalize_phrase(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().strip("'\"`").lower())
