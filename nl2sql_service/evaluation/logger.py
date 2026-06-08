from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonlFailureLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        self._handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "JsonlFailureLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

