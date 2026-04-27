import json
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

COLLECTION = "webportal_nl2sql_context"
VECTOR_SIZE = 768  # replace with your embedding size


def embed(text: str) -> list[float]:
    raise NotImplementedError("Connect your embedding model here")


def main() -> None:
    client = QdrantClient(url="http://localhost:6333")

    if not client.collection_exists(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )

    corpus_path = Path("/opt/webportal/docs/nl2sql-corpus.jsonl")
    points = []

    for idx, line in enumerate(corpus_path.read_text(encoding="utf-8").splitlines(), start=1):
        row = json.loads(line)
        text = row["text"]
        vector = embed(text)
        payload = {
            "id": row["id"],
            "title": row["title"],
            "text": text,
            "tags": row.get("tags", []),
            "source_file": "docs/nl2sql-corpus.jsonl",
        }
        points.append(PointStruct(id=idx, vector=vector, payload=payload))

    client.upsert(collection_name=COLLECTION, points=points)
    print(f"Indexed {len(points)} chunks into {COLLECTION}")


if __name__ == "__main__":
    main()
