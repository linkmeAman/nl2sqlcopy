"""
nl2sql_service/log_config.py
============================
JSON-structured logging with async-safe request_id propagation.

Every log line emitted after configure_logging() is called will be valid JSON:

    {"ts": "2026-04-30T10:00:00.123456Z", "level": "INFO",
     "logger": "nl2sql_service.main", "request_id": "abc123", "msg": "..."}

Usage in main.py
----------------
    from nl2sql_service.log_config import configure_logging, set_request_id

    configure_logging()          # call once at module level

    # Inside each request handler, after resolving request_id:
    set_request_id(request_id)
    # All log calls from here — including sub-module calls — carry that id.

Reading logs from journalctl
----------------------------
    journalctl -u nl2sql -f | python3 -m json.tool
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Context variable — one value per async task (request)
# ---------------------------------------------------------------------------

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(rid: str) -> None:
    """Set the request_id for the current async context."""
    _request_id_var.set(rid if rid else "-")


def get_request_id() -> str:
    """Return the request_id for the current async context."""
    return _request_id_var.get()


# ---------------------------------------------------------------------------
# Filter — injects request_id into every LogRecord
# ---------------------------------------------------------------------------


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()  # type: ignore[attr-defined]
        return True


# ---------------------------------------------------------------------------
# Formatter — emits each record as a single-line JSON object
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload: dict[str, object] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public setup function
# ---------------------------------------------------------------------------


def configure_logging(level: int = logging.INFO) -> None:
    """
    Replace the root logger's handlers with a single StreamHandler that
    writes JSON lines to stdout, and attach the request_id filter.

    Safe to call multiple times (idempotent after the first call).
    """
    root = logging.getLogger()

    # Remove any handlers that the previous basicConfig or pytest may have set.
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_RequestIdFilter())

    root.addHandler(handler)
    root.setLevel(level)
