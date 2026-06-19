from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from nl2sql_service.llm.interfaces import LLMChunk, LLMRequest, LLMResponse


class LLMClient(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse:
        ...

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        ...
