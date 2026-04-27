import argparse

from fastembed import TextEmbedding
from qdrant_client import QdrantClient


DEFAULT_COLLECTION = "webportal_nl2sql_context"
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def main() -> None:
    parser = argparse.ArgumentParser(description="Query NL2SQL context from Qdrant using FastEmbed.")
    parser.add_argument("question", help="Natural-language question")
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    client = QdrantClient(url=args.qdrant_url)
    embedder = TextEmbedding(model_name=args.model)
    query_vector = next(embedder.embed([args.question])).tolist()
    hits = client.query_points(
        collection_name=args.collection,
        query=query_vector,
        limit=args.limit,
        with_payload=True,
    ).points

    for idx, hit in enumerate(hits, start=1):
        payload = hit.payload or {}
        print(f"[{idx}] score={hit.score:.4f}")
        print(f"title: {payload.get('title', '')}")
        print(f"doc_id: {payload.get('doc_id', '')}")
        print(f"tags: {payload.get('tags', [])}")
        print(f"text: {payload.get('text', '')}")
        print("-" * 80)


if __name__ == "__main__":
    main()
