from __future__ import annotations

import hashlib
import time
from typing import Any

from nl2sql_service.config import settings


EMBED_CACHE_TTL = settings.embed_cache_ttl_seconds
SQL_CACHE_TTL = settings.sql_cache_ttl_seconds


class EmbedCache:
    _store: dict[str, tuple[list[float], float]] = {}
    _max_size: int = 500

    def get(self, text: str) -> list[float] | None:
        key = hashlib.md5(text.encode()).hexdigest()
        entry = self._store.get(key)
        if not entry:
            return None
        vector, ts = entry
        if time.time() - ts > EMBED_CACHE_TTL:
            del self._store[key]
            return None
        return vector

    def set(self, text: str, vector: list[float]) -> None:
        key = hashlib.md5(text.encode()).hexdigest()
        if len(self._store) >= self._max_size:
            oldest_key = min(self._store, key=lambda item: self._store[item][1])
            del self._store[oldest_key]
        self._store[key] = (vector, time.time())

    def size(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()


class SqlCache:
    _store: dict[str, tuple[dict[str, Any], float]] = {}
    _max_size: int = 200

    def get(self, query: str, top_k: int) -> dict[str, Any] | None:
        key = self._key(query, top_k)
        entry = self._store.get(key)
        if not entry:
            return None
        result, ts = entry
        if time.time() - ts > SQL_CACHE_TTL:
            del self._store[key]
            return None
        return dict(result)

    def set(self, query: str, top_k: int, result: dict[str, Any]) -> None:
        if result.get("status") != "ok":
            return
        key = self._key(query, top_k)
        if len(self._store) >= self._max_size:
            oldest_key = min(self._store, key=lambda item: self._store[item][1])
            del self._store[oldest_key]
        self._store[key] = (dict(result), time.time())

    def invalidate_all(self) -> int:
        count = len(self._store)
        self._store.clear()
        return count

    def size(self) -> int:
        return len(self._store)

    @staticmethod
    def _key(query: str, top_k: int) -> str:
        return hashlib.md5(f"{query.strip().lower()}:{top_k}".encode()).hexdigest()


embed_cache = EmbedCache()
sql_cache = SqlCache()


def cache_stats() -> dict[str, int]:
    return {
        "embed_cache_size": embed_cache.size(),
        "sql_cache_size": sql_cache.size(),
        "embed_cache_ttl_seconds": EMBED_CACHE_TTL,
        "sql_cache_ttl_seconds": SQL_CACHE_TTL,
    }
