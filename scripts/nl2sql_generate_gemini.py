import argparse
import json
import os
import urllib.error
import urllib.request

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue


DEFAULT_COLLECTION = "webportal_nl2sql_context"
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_LIMIT = 8
DEFAULT_MIN_SCORE = 0.35
DEFAULT_CANDIDATE_MULTIPLIER = 4
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_COLUMNS_SOURCE_SUBSTRINGS = [
    "nl2sql-columns.jsonl",
    "nl2sql_schema_views.jsonl",
]
DEFAULT_REQUIRED_DOC_IDS = [
    "workspace_overview",
    "default_filters_and_flags",
    "nl2sql_guardrails",
    "common_wrong_to_correct_columns",
    "validation_first_nl2sql_workflow",
]


SYSTEM_INSTRUCTIONS = """You are an NL-to-SQL assistant for the WebPortal workspace.

Write MySQL SQL for this codebase using the retrieved context.

Rules:
- Prefer views when they already contain the requested business meaning.
- Do not invent foreign keys unless the context explicitly proves them.
- Do not invent column names. Use only columns explicitly present in the retrieved context.
- If retrieved context includes a column catalog or wrong-versus-correct mapping, treat that as authoritative.
- Qualify schema names when mixing pf_TickleRight_9210, pf_central, and pf_admin.
- Treat request.table_name plus request.row_id as a polymorphic application reference, not a fixed foreign key.
- If a question is about active operational data, prefer park = 0 unless the user asks for archived data.
- If the request is ambiguous, choose the safest query shape and mention the assumption briefly.
- If you do not have enough column evidence for a field, do not guess. Use the closest proven column and explain it briefly in Assumptions.
- Return SQL first. After SQL, include a short note named Assumptions.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retrieve NL2SQL context from Qdrant and send it to Gemini."
    )
    parser.add_argument("question", help="Natural-language analytics or reporting question")
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--gemini-model", default=DEFAULT_GEMINI_MODEL)
    parser.add_argument("--gemini-api-key", default=os.getenv("GEMINI_API_KEY"))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument(
        "--min-score",
        type=float,
        default=DEFAULT_MIN_SCORE,
        help="Minimum semantic score for primary context selection.",
    )
    parser.add_argument(
        "--candidate-multiplier",
        type=int,
        default=DEFAULT_CANDIDATE_MULTIPLIER,
        help="Retrieve more candidate chunks before post-filtering.",
    )
    parser.add_argument(
        "--require-columns-context",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Force inclusion of at least one columns-catalog chunk when available.",
    )
    parser.add_argument(
        "--columns-source-substring",
        action="append",
        default=list(DEFAULT_COLUMNS_SOURCE_SUBSTRINGS),
        help="Substring used to identify column-catalog rows by source_file. Repeatable.",
    )
    parser.add_argument(
        "--required-doc-id",
        action="append",
        default=list(DEFAULT_REQUIRED_DOC_IDS),
        help="Doc ID that should always be included when present in Qdrant. Repeatable.",
    )
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="Print retrieved context before the Gemini response",
    )
    return parser.parse_args()


def context_item_from_payload(payload: dict, score: float | None) -> dict:
    return {
        "score": None if score is None else round(score, 4),
        "doc_id": payload.get("doc_id", ""),
        "title": payload.get("title", ""),
        "tags": payload.get("tags", []),
        "text": payload.get("text", ""),
        "source_file": payload.get("source_file", ""),
    }


def is_columns_chunk(item: dict, columns_source_substrings: list[str]) -> bool:
    source = str(item.get("source_file", "")).lower()
    return any(substring.lower() in source for substring in columns_source_substrings)


def fetch_required_docs(client: QdrantClient, collection: str, doc_ids: list[str]) -> list[dict]:
    required = []
    for doc_id in doc_ids:
        points, _ = client.scroll(
            collection_name=collection,
            scroll_filter=Filter(
                must=[
                    FieldCondition(
                        key="doc_id",
                        match=MatchValue(value=doc_id),
                    )
                ]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if points:
            payload = points[0].payload or {}
            required.append(context_item_from_payload(payload, score=None))
    return required


def dedupe_context(context: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for item in context:
        key = (
            item.get("doc_id", ""),
            item.get("source_file", ""),
            item.get("title", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def choose_semantic_context(
    candidates: list[dict],
    limit: int,
    min_score: float,
    require_columns_context: bool,
    columns_source_substrings: list[str],
) -> list[dict]:
    high_confidence = [
        item for item in candidates if item.get("score") is not None and item["score"] >= min_score
    ]
    working_set = high_confidence if high_confidence else candidates

    selected = []
    if require_columns_context:
        best_column = next(
            (item for item in working_set if is_columns_chunk(item, columns_source_substrings)),
            None,
        )
        if best_column is None:
            best_column = next(
                (item for item in candidates if is_columns_chunk(item, columns_source_substrings)),
                None,
            )
        if best_column is not None:
            selected.append(best_column)

    for item in working_set:
        if len(selected) >= limit:
            break
        selected.append(item)

    if len(selected) < limit:
        for item in candidates:
            if len(selected) >= limit:
                break
            selected.append(item)

    return dedupe_context(selected)[:limit]


def retrieve_context(
    qdrant_url: str,
    collection: str,
    embed_model: str,
    question: str,
    limit: int,
    min_score: float,
    candidate_multiplier: int,
    require_columns_context: bool,
    columns_source_substrings: list[str],
    required_doc_ids: list[str],
) -> list[dict]:
    client = QdrantClient(url=qdrant_url)
    embedder = TextEmbedding(model_name=embed_model)
    query_vector = next(embedder.embed([question])).tolist()

    candidate_limit = max(limit, limit * max(candidate_multiplier, 1))
    hits = client.query_points(
        collection_name=collection,
        query=query_vector,
        limit=candidate_limit,
        with_payload=True,
    ).points

    semantic_candidates = []
    for hit in hits:
        payload = hit.payload or {}
        semantic_candidates.append(
            context_item_from_payload(
                payload=payload,
                score=hit.score,
            )
        )

    required = fetch_required_docs(client=client, collection=collection, doc_ids=required_doc_ids)
    semantic = choose_semantic_context(
        candidates=semantic_candidates,
        limit=limit,
        min_score=min_score,
        require_columns_context=require_columns_context,
        columns_source_substrings=columns_source_substrings,
    )
    return dedupe_context(required + semantic)


def build_prompt(question: str, context: list[dict]) -> str:
    context_blocks = []
    for idx, item in enumerate(context, start=1):
        tags = ", ".join(item["tags"]) if item["tags"] else ""
        score_text = "static" if item.get("score") is None else str(item["score"])
        block = (
            f"[Context {idx}]\n"
            f"Title: {item['title']}\n"
            f"Doc ID: {item['doc_id']}\n"
            f"Score: {score_text}\n"
            f"Tags: {tags}\n"
            f"Source: {item['source_file']}\n"
            f"Text: {item['text']}"
        )
        context_blocks.append(block)

    return (
        f"{SYSTEM_INSTRUCTIONS}\n"
        f"User Question:\n{question}\n\n"
        f"Retrieved Context:\n\n" + "\n\n".join(context_blocks)
    )


def call_gemini(model: str, api_key: str, prompt: str) -> dict:
    url = f"{GEMINI_API_BASE}/{model}:generateContent"
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt,
                    }
                ]
            }
        ]
    }

    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Gemini API HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Gemini API connection failed: {exc}") from exc


def extract_text(response: dict) -> str:
    candidates = response.get("candidates") or []
    parts = []
    for candidate in candidates:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            text = part.get("text")
            if text:
                parts.append(text)
    if parts:
        return "\n".join(parts).strip()

    prompt_feedback = response.get("promptFeedback")
    if prompt_feedback:
        return f"No text returned. promptFeedback={json.dumps(prompt_feedback, ensure_ascii=False)}"

    return f"No text returned. Raw response={json.dumps(response, ensure_ascii=False)}"


def main() -> None:
    args = parse_args()
    if not args.gemini_api_key:
        raise SystemExit("Missing Gemini API key. Pass --gemini-api-key or set GEMINI_API_KEY.")

    context = retrieve_context(
        qdrant_url=args.qdrant_url,
        collection=args.collection,
        embed_model=args.embed_model,
        question=args.question,
        limit=args.limit,
        min_score=args.min_score,
        candidate_multiplier=args.candidate_multiplier,
        require_columns_context=args.require_columns_context,
        columns_source_substrings=args.columns_source_substring,
        required_doc_ids=args.required_doc_id,
    )
    prompt = build_prompt(args.question, context)

    if args.show_context:
        print("=== Retrieved Context ===")
        print(json.dumps(context, indent=2, ensure_ascii=False))
        print("=== End Context ===")

    response = call_gemini(args.gemini_model, args.gemini_api_key, prompt)
    print(extract_text(response))


if __name__ == "__main__":
    main()
