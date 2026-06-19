from __future__ import annotations

from enum import Enum


class LLMRole(str, Enum):
    SQL = "sql"
    REASONING = "reasoning"
    QUERY_REWRITE = "query_rewrite"
    ANSWER = "answer"
    EMBEDDING = "embedding"
    DEFAULT = "default"


ALL_ROLES = [role.value for role in LLMRole]
GENERATION_ROLES = [
    LLMRole.SQL,
    LLMRole.REASONING,
    LLMRole.QUERY_REWRITE,
    LLMRole.ANSWER,
]
