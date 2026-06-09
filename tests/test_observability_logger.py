from __future__ import annotations

import logging

from nl2sql_service.observability.logger import _ImportantLogFilter


def test_important_log_filter_keeps_major_events_and_drops_noise() -> None:
    filter_ = _ImportantLogFilter()

    important = logging.LogRecord(
        name="nl2sql_service.observability.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="SQL generated",
        args=(),
        exc_info=None,
    )
    important.observability_payload = {"stage": "sql_generation", "status": "completed"}

    noise = logging.LogRecord(
        name="nl2sql_service.observability.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=20,
        msg="cache lookup",
        args=(),
        exc_info=None,
    )
    noise.observability_payload = {"stage": "cache_lookup", "status": "completed"}

    assert filter_.filter(important) is True
    assert filter_.filter(noise) is False


def test_important_log_filter_always_keeps_warnings_and_errors() -> None:
    filter_ = _ImportantLogFilter()

    warning = logging.LogRecord(
        name="nl2sql_service.observability.logger",
        level=logging.WARNING,
        pathname=__file__,
        lineno=30,
        msg="queue full",
        args=(),
        exc_info=None,
    )

    error = logging.LogRecord(
        name="nl2sql_service.observability.logger",
        level=logging.ERROR,
        pathname=__file__,
        lineno=40,
        msg="persist failed",
        args=(),
        exc_info=None,
    )

    assert filter_.filter(warning) is True
    assert filter_.filter(error) is True
