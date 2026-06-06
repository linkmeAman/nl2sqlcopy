from __future__ import annotations

import hashlib
import math
import time
from typing import Any

from nl2sql_service.config import settings


EMBED_CACHE_TTL = settings.embed_cache_ttl_seconds
SQL_CACHE_TTL = settings.sql_cache_ttl_seconds
ASK_CACHE_TTL = settings.ask_cache_ttl_seconds


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


class SemanticSqlCache:
    """
    Like SqlCache but also allows lookup by cosine similarity of the query
    embedding.  Falls back to exact-match first; semantic search is only used
    when ``semantic_threshold`` is provided at lookup time.

    Stores: { md5_key -> (result, embedding_vector, ts) }
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[dict[str, Any], list[float] | None, float]] = {}
        self._max_size: int = 200

    def get_exact(self, query: str, top_k: int) -> dict[str, Any] | None:
        key = self._key(query, top_k)
        entry = self._store.get(key)
        if not entry:
            return None
        result, _, ts = entry
        if time.time() - ts > SQL_CACHE_TTL:
            del self._store[key]
            return None
        return dict(result)

    def get_semantic(
        self,
        query_embedding: list[float],
        top_k: int,
        threshold: float | None = None,
    ) -> dict[str, Any] | None:
        """Return the best cached result whose stored embedding has cosine
        similarity >= *threshold* with *query_embedding* and the same top_k."""
        threshold = settings.sql_cache_semantic_threshold if threshold is None else threshold
        now = time.time()
        best_score = -1.0
        best_result: dict[str, Any] | None = None
        stale_keys: list[str] = []

        for key, (result, vec, ts) in self._store.items():
            if time.time() - ts > SQL_CACHE_TTL:
                stale_keys.append(key)
                continue
            if vec is None:
                continue
            stored_top_k = result.get("_top_k")
            if stored_top_k is not None and stored_top_k != top_k:
                continue
            score = _cosine(query_embedding, vec)
            if score >= threshold and score > best_score:
                best_score = score
                best_result = dict(result)

        for k in stale_keys:
            self._store.pop(k, None)

        return best_result

    def set(self, query: str, top_k: int, result: dict[str, Any], embedding: list[float] | None = None) -> None:
        if result.get("status") != "ok":
            return
        key = self._key(query, top_k)
        if len(self._store) >= self._max_size:
            oldest_key = min(self._store, key=lambda item: self._store[item][2])
            del self._store[oldest_key]
        payload = dict(result)
        payload["_top_k"] = top_k
        self._store[key] = (payload, embedding, time.time())

    def invalidate_all(self) -> int:
        count = len(self._store)
        self._store.clear()
        return count

    def size(self) -> int:
        return len(self._store)

    @staticmethod
    def _key(query: str, top_k: int) -> str:
        return hashlib.md5(f"{query.strip().lower()}:{top_k}".encode()).hexdigest()


class AskCache:
    """
    Full-response cache for /ask.

    Keyed on normalized query + top_k (exact match).  Stores the serialised
    AskSuccess payload so a repeated identical query returns immediately without
    touching Ollama, pgvector, or MySQL.

    Supports a secondary semantic lookup via cosine similarity of the stored
    query embedding against the incoming query embedding.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[dict[str, Any], list[float] | None, float]] = {}
        self._max_size: int = 200

    def get_exact(self, query: str, top_k: int) -> dict[str, Any] | None:
        key = self._key(query, top_k)
        entry = self._store.get(key)
        if not entry:
            return None
        result, _, ts = entry
        if time.time() - ts > ASK_CACHE_TTL:
            del self._store[key]
            return None
        return dict(result)

    def get_semantic(
        self,
        query_embedding: list[float],
        top_k: int,
        threshold: float | None = None,
    ) -> dict[str, Any] | None:
        """Return the best cached ask result whose stored embedding has cosine
        similarity >= *threshold* with *query_embedding* and the same top_k."""
        threshold = settings.ask_cache_semantic_threshold if threshold is None else threshold
        best_score = -1.0
        best_result: dict[str, Any] | None = None
        stale_keys: list[str] = []

        for key, (result, vec, ts) in self._store.items():
            if time.time() - ts > ASK_CACHE_TTL:
                stale_keys.append(key)
                continue
            if vec is None:
                continue
            stored_top_k = result.get("_top_k")
            if stored_top_k is not None and stored_top_k != top_k:
                continue
            score = _cosine(query_embedding, vec)
            if score >= threshold and score > best_score:
                best_score = score
                best_result = dict(result)

        for k in stale_keys:
            self._store.pop(k, None)

        return best_result

    def set(self, query: str, top_k: int, result: dict[str, Any], embedding: list[float] | None = None) -> None:
        if result.get("status") != "ok":
            return
        key = self._key(query, top_k)
        if len(self._store) >= self._max_size:
            oldest_key = min(self._store, key=lambda item: self._store[item][2])
            del self._store[oldest_key]
        payload = dict(result)
        payload["_top_k"] = top_k
        self._store[key] = (payload, embedding, time.time())

    def invalidate_all(self) -> int:
        count = len(self._store)
        self._store.clear()
        return count

    def size(self) -> int:
        return len(self._store)

    @staticmethod
    def _key(query: str, top_k: int) -> str:
        return hashlib.md5(f"{query.strip().lower()}:{top_k}".encode()).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity — only used for small in-memory caches."""
    if len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


embed_cache = EmbedCache()
sql_cache = SqlCache()
semantic_sql_cache = SemanticSqlCache()
ask_cache = AskCache()


def cache_stats() -> dict[str, int]:
    return {
        "embed_cache_size": embed_cache.size(),
        "sql_cache_size": sql_cache.size(),
        "semantic_sql_cache_size": semantic_sql_cache.size(),
        "ask_cache_size": ask_cache.size(),
        "embed_cache_ttl_seconds": EMBED_CACHE_TTL,
        "sql_cache_ttl_seconds": SQL_CACHE_TTL,
        "ask_cache_ttl_seconds": ASK_CACHE_TTL,
    }


def clear_memory_caches() -> dict[str, int]:
    embed_cleared = embed_cache.size()
    sql_cleared = sql_cache.invalidate_all()
    semantic_sql_cleared = semantic_sql_cache.invalidate_all()
    ask_cleared = ask_cache.invalidate_all()
    embed_cache.clear()
    return {
        "embed_cleared": embed_cleared,
        "sql_cleared": sql_cleared,
        "semantic_sql_cleared": semantic_sql_cleared,
        "ask_cleared": ask_cleared,
    }
