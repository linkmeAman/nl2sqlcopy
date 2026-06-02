from __future__ import annotations

import hashlib
import re
from typing import Any

_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "password",
    "secret",
    "token",
    "cookie",
    "credential",
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(bearer\s+[a-z0-9._-]+|sk-[a-z0-9]{8,}|api[_-]?key\s*[:=]\s*[^\s,;]+)\b"
)


def redact_secret(value: str) -> str:
    return _SECRET_VALUE_RE.sub("[REDACTED]", value)


def sanitize_text(value: str | None, *, limit: int = 4000) -> str | None:
    if value is None:
        return None
    redacted = redact_secret(str(value))
    if len(redacted) <= limit:
        return redacted
    return f"{redacted[:limit]}...<trimmed>"


def summarize_text(value: str | None, *, limit: int = 240) -> str | None:
    sanitized = sanitize_text(value, limit=limit)
    if sanitized is None:
        return None
    return " ".join(sanitized.split())


def stable_hash(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sanitize_value(value: Any, *, string_limit: int = 500) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return sanitize_text(value, limit=string_limit)
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            if any(fragment in key_text.lower() for fragment in _SECRET_KEY_FRAGMENTS):
                sanitized[key_text] = "[REDACTED]"
            else:
                sanitized[key_text] = sanitize_value(nested, string_limit=string_limit)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [sanitize_value(item, string_limit=string_limit) for item in value]
    return sanitize_text(str(value), limit=string_limit)


def sanitize_sql(value: str | None, *, limit: int = 1000) -> str | None:
    return sanitize_text(value, limit=limit)

