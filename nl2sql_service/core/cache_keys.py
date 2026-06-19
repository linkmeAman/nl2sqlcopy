from __future__ import annotations

import re

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_TOKEN_CANONICAL_MAP = {
    "newest": "latest",
    "recent": "latest",
    "recently": "latest",
    "show": "",
    "list": "",
    "give": "",
    "tell": "",
    "me": "",
    "what": "",
    "which": "",
    "is": "",
    "are": "",
    "the": "",
    "a": "",
    "an": "",
    "please": "",
}


def canonicalize_query_text(query: str) -> str:
    text = _NON_ALNUM_RE.sub(" ", (query or "").strip().lower())
    tokens = []
    for token in text.split():
        canonical = _TOKEN_CANONICAL_MAP.get(token, token)
        if not canonical:
            continue
        tokens.append(canonical)
    return " ".join(tokens)
