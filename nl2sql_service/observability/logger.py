from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from nl2sql_service.observability.context import get_observability_context
from nl2sql_service.observability.metrics import observe_stage

logger = logging.getLogger(__name__)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = get_observability_context()
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service": context.service,
            "logger": record.name,
            "module": record.module,
            "request_id": context.request_id,
            "trace_id": context.trace_id or None,
            "correlation_id": context.correlation_id or None,
            "session_id": context.session_id or None,
            "workflow_id": context.workflow_id or None,
            "message": record.getMessage(),
        }
        extra_payload = getattr(record, "observability_payload", None)
        if isinstance(extra_payload, dict):
            payload.update(extra_payload)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _build_file_handler(
    *,
    log_dir: Path,
    filename: str,
    retention_days: int,
) -> logging.Handler:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(
        filename=str(log_dir / filename),
        when="midnight",
        interval=1,
        backupCount=max(retention_days, 1),
        encoding="utf-8",
        utc=False,
    )
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(_JsonFormatter())
    return handler


def configure_logging(
    *,
    level: int = logging.INFO,
    enable_file_logging: bool = True,
    log_dir: Path | None = None,
    log_filename: str = "nl2sql.log",
    retention_days: int = 30,
) -> None:
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(_JsonFormatter())
    root.addHandler(stdout_handler)
    if enable_file_logging:
        resolved_dir = log_dir or (Path.cwd() / "logs")
        root.addHandler(
            _build_file_handler(
                log_dir=resolved_dir,
                filename=log_filename,
                retention_days=retention_days,
            )
        )
    root.setLevel(level)


class AsyncObservabilityPipeline:
    def __init__(
        self,
        *,
        pool: Any,
        persist_event,
        queue_size: int = 5000,
        batch_size: int = 50,
        flush_interval_seconds: float = 0.2,
    ) -> None:
        self.pool = pool
        self.persist_event = persist_event
        self.queue_size = queue_size
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=queue_size)
        self._worker: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._closing = False
            self._worker = asyncio.create_task(self._run(), name="nl2sql-observability")

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._closing = True
        await self.queue.put(None)
        await self._worker
        self._worker = None

    async def publish(self, event: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "Dropping observability event because the queue is full.",
                extra={
                    "observability_payload": {
                        "event": "observability_queue_full",
                        "stage": "observability_pipeline",
                        "status": "warning",
                    }
                },
            )

    async def _run(self) -> None:
        batch: list[dict[str, Any]] = []
        while True:
            try:
                item = await asyncio.wait_for(
                    self.queue.get(),
                    timeout=self.flush_interval_seconds,
                )
            except asyncio.TimeoutError:
                item = ...

            if item is ...:
                if batch:
                    await self._flush(batch)
                    batch = []
                continue
            if item is None:
                if batch:
                    await self._flush(batch)
                return

            batch.append(item)
            if len(batch) >= self.batch_size:
                await self._flush(batch)
                batch = []

    async def _flush(self, batch: list[dict[str, Any]]) -> None:
        for event in batch:
            observe_stage(
                str(event.get("stage", "unknown")),
                str(event.get("status", "unknown")),
                event.get("duration_ms") if isinstance(event.get("duration_ms"), int) else None,
            )
            logger.info(
                event.get("message", "observability_event"),
                extra={"observability_payload": event},
            )
            if self.pool is not None and hasattr(self.pool, "acquire"):
                try:
                    await self.persist_event(self.pool, event)
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to persist observability event")
