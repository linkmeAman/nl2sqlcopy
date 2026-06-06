from __future__ import annotations

import logging

from nl2sql_service.observability.context import set_request_id as _set_request_id
from nl2sql_service.observability.logger import configure_logging


def set_request_id(rid: str) -> None:
    _set_request_id(rid)


def get_request_id() -> str:
    from nl2sql_service.observability.context import get_observability_context

    return get_observability_context().request_id


__all__ = ["configure_logging", "get_request_id", "set_request_id", "logging"]
