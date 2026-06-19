from __future__ import annotations

import json
from collections.abc import AsyncIterator

from nl2sql_service.llm.interfaces import GenerateInput, LLMProvider


async def sse_stream(
    provider: LLMProvider,
    input_: GenerateInput,
) -> AsyncIterator[bytes]:
    async for chunk in provider.stream(input_):
        yield f"data: {json.dumps({'delta': chunk})}\n\n".encode("utf-8")
    yield b"data: {\"done\": true}\n\n"
