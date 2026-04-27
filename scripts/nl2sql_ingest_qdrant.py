import argparse
import json
from pathlib import Path

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams


DEFAULT_COLLECTION = "webportal_nl2sql_context"
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def load_manifest_corpora(manifest_path: Path) -> list[Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_dir = manifest_path.parent
    files = manifest.get("files", {})
    return [base_dir / file_name for file_name in files.values()]


def resolve_corpus_paths(corpus_args: list[str], manifest_args: list[str]) -> list[Path]:
    paths = [Path(path).resolve() for path in corpus_args]
    for manifest_arg in manifest_args:
        manifest_path = Path(manifest_arg).resolve()
        paths.extend(path.resolve() for path in load_manifest_corpora(manifest_path))

    deduped: list[Path] = []
    seen = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def load_corpora(paths: list[Path]) -> list[tuple[dict, Path]]:
    items: list[tuple[dict, Path]] = []
    for path in paths:
        rows = load_jsonl(path)
        for row in rows:
            items.append((row, path))
    return items


def ensure_collection(client: QdrantClient, collection: str, vector_size: int) -> None:
    collections = {c.name for c in client.get_collections().collections}
    if collection not in collections:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )


def build_payload(row: dict, source_file: str) -> dict:
    return {
        "doc_id": row.get("id", ""),
        "title": row.get("title", ""),
        "tags": row.get("tags", []),
        "text": row.get("text", ""),
        "source_file": source_file,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest NL2SQL corpus into Qdrant using FastEmbed.")
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--corpus",
        action="append",
        default=[],
        help="Path to a JSONL corpus file. Repeat --corpus to index multiple files.",
    )
    parser.add_argument(
        "--manifest",
        action="append",
        default=[],
        help="Path to a generated corpus manifest. All listed JSONL files will be indexed.",
    )
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()

    corpus_paths = resolve_corpus_paths(args.corpus, args.manifest)
    if not corpus_paths:
        raise SystemExit("Provide at least one --corpus or --manifest input.")
    items = load_corpora(corpus_paths)
    if not items:
        raise SystemExit(f"No rows found in corpus files: {', '.join(str(path) for path in corpus_paths)}")

    documents = [row["text"] for row, _ in items]
    payloads = [build_payload(row, str(path)) for row, path in items]
    ids = list(range(1, len(items) + 1))

    client = QdrantClient(url=args.qdrant_url)
    embedder = TextEmbedding(model_name=args.model)
    vectors = [vector.tolist() for vector in embedder.embed(documents)]
    ensure_collection(client, args.collection, len(vectors[0]))

    client.upload_collection(
        collection_name=args.collection,
        vectors=vectors,
        payload=payloads,
        ids=ids,
        batch_size=args.batch_size,
    )

    print(f"Indexed {len(items)} rows into collection '{args.collection}' from {len(corpus_paths)} corpus file(s)")


if __name__ == "__main__":
    main()
