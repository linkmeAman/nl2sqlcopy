from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nl2sql_service.config import settings
from nl2sql_service.llm.factory import LLMFactory

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exception hierarchy
# ---------------------------------------------------------------------------


class EmbeddingError(Exception):
    """Base class for all embedding errors."""


class EmbeddingTimeoutError(EmbeddingError):
    """Raised when the embedding server does not respond in time."""


class EmbeddingUpstreamError(EmbeddingError):
    """Raised on HTTP 5xx from the embedding server (retried)."""


class EmbeddingClientError(EmbeddingError):
    """Raised on HTTP 4xx from the embedding server (not retried)."""


class EmbeddingDimensionError(EmbeddingError):
    """Raised when the returned vector dimension does not match config."""


# ---------------------------------------------------------------------------
# Shared async client lifecycle
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None


async def init_client() -> None:
    global _client
    _client = httpx.AsyncClient(timeout=settings.embed_timeout)
    logger.info("Embedding HTTP client initialised (timeout=%.1fs)", settings.embed_timeout)


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("Embedding HTTP client closed")


def _get_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("Embedding client not initialised. Call init_client() first.")
    return _client


# ---------------------------------------------------------------------------
# Internal: single batch call with retry
# ---------------------------------------------------------------------------


def _build_retry_decorator():
    """Build the tenacity retry decorator from live settings values."""
    return retry(
        retry=retry_if_exception_type((EmbeddingUpstreamError, EmbeddingTimeoutError)),
        stop=stop_after_attempt(settings.embed_max_retries),
        wait=wait_exponential(
            multiplier=settings.embed_retry_base_delay,
            min=settings.embed_retry_base_delay,
            max=settings.embed_retry_base_delay * 16,
        ),
        reraise=True,
    )


async def _call_custom_embed_api(texts: list[str]) -> list[list[float]]:
    """
    POST ``{"texts": texts}`` to the embedding endpoint.
    Validates the response shape and vector dimensions.
    """
    client = _get_client()
    try:
        response = await client.post(
            settings.embedding_api_url,
            json={"inputs": texts},
        )
    except httpx.TimeoutException as exc:
        raise EmbeddingTimeoutError(
            f"Embedding server timed out after {settings.embed_timeout}s"
        ) from exc
    except httpx.RequestError as exc:
        raise EmbeddingUpstreamError(f"Network error reaching embedding server: {exc}") from exc

    if response.status_code >= 500:
        raise EmbeddingUpstreamError(
            f"Embedding server returned HTTP {response.status_code}: {response.text[:200]}"
        )
    if response.status_code >= 400:
        raise EmbeddingClientError(
            f"Embedding server returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        payload = response.json()
        # TEI returns a bare array of vectors; a wrapped {"embeddings": [...]} shape
        # is also accepted for forward compatibility.
        if isinstance(payload, list):
            embeddings: list[list[float]] = payload
        else:
            embeddings = payload["embeddings"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingClientError(
            f"Unexpected embedding response shape: {response.text[:200]}"
        ) from exc

    if len(embeddings) != len(texts):
        raise EmbeddingClientError(
            f"Embedding count mismatch: sent {len(texts)}, received {len(embeddings)}"
        )

    _validate_embeddings(texts, embeddings)
    return embeddings


def _is_custom_provider() -> bool:
    return settings.embedding_provider.strip().lower() in {"custom", "http", "tei", "external"}


def _validate_embeddings(texts: list[str], embeddings: list[list[float]]) -> None:
    if len(embeddings) != len(texts):
        raise EmbeddingClientError(
            f"Embedding count mismatch: sent {len(texts)}, received {len(embeddings)}"
        )

    for i, vec in enumerate(embeddings):
        if len(vec) != settings.embedding_dimension:
            raise EmbeddingDimensionError(
                f"Vector {i} has dimension {len(vec)}, expected {settings.embedding_dimension}. "
                f"Check EMBEDDING_DIMENSION in config."
            )


async def _call_embed_api(texts: list[str]) -> list[list[float]]:
    if _is_custom_provider():
        return await _call_custom_embed_api(texts)

    try:
        provider = LLMFactory.create_embedding_provider(settings)
        embeddings = await provider.embeddings(texts)
    except TimeoutError as exc:
        raise EmbeddingTimeoutError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingUpstreamError(
            f"Embedding provider {settings.embedding_provider} failed: {exc}"
        ) from exc

    _validate_embeddings(texts, embeddings)
    return embeddings


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Embed a single batch with exponential-backoff retry on transient failures.

    Raises:
        EmbeddingTimeoutError: server did not respond within the timeout (after all retries).
        EmbeddingUpstreamError: server returned 5xx (after all retries).
        EmbeddingClientError: server returned 4xx or malformed response (not retried).
        EmbeddingDimensionError: returned vectors have wrong dimension (not retried).
    """
    retrying = _build_retry_decorator()

    @retrying
    async def _with_retry() -> list[list[float]]:
        return await _call_embed_api(texts)

    return await _with_retry()


# ---------------------------------------------------------------------------
# Public: chunked batch driver
# ---------------------------------------------------------------------------


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Embed an arbitrary list of texts by splitting into ``BATCH_SIZE`` slices.

    Returns vectors in the same order as the input list.
    """
    if not texts:
        return []

    results: list[list[float]] = []
    batch_size = settings.batch_size

    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        logger.debug("Embedding batch %d–%d of %d", start, start + len(batch) - 1, len(texts))
        batch_vectors = await embed_batch(batch)
        results.extend(batch_vectors)

    return results
