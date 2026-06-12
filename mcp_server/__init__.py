from __future__ import annotations

from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_EXTERNAL_SOURCE_ROOT = _PACKAGE_DIR.parent.parent / "nl2sql-mcp"

# Resolve mcp_server.* imports from this repo first, then from the sibling
# nl2sql-mcp source tree where the actual MCP implementation lives.
__path__ = [str(_PACKAGE_DIR)]
if _EXTERNAL_SOURCE_ROOT.is_dir():
    __path__.append(str(_EXTERNAL_SOURCE_ROOT))

