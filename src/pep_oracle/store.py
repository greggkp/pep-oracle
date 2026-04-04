import chromadb

from pep_oracle.config import CHROMA_COLLECTION, CHROMA_DIR, ensure_dirs
from pep_oracle.models import Chunk

SENTINEL_NO_TIME = -1.0


def get_client(persistent: bool = True) -> chromadb.ClientAPI:
    if persistent:
        ensure_dirs()
        return chromadb.PersistentClient(path=str(CHROMA_DIR))
    return chromadb.Client()


def get_collection(client: chromadb.ClientAPI) -> chromadb.Collection:
    return client.get_or_create_collection(
        name=CHROMA_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )


def add_chunks(
    collection: chromadb.Collection,
    chunks: list[Chunk],
    embeddings: list[list[float]],
) -> None:
    collection.upsert(
        ids=[c.chunk_id for c in chunks],
        embeddings=embeddings,
        documents=[c.text for c in chunks],
        metadatas=[_chunk_metadata(c) for c in chunks],
    )


def query(
    collection: chromadb.Collection,
    embedding: list[float],
    top_k: int = 10,
    episode_number: int | None = None,
) -> list[dict]:
    where = {"episode_number": episode_number} if episode_number else None
    results = collection.query(
        query_embeddings=[embedding],
        n_results=top_k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    items = []
    for i in range(len(results["ids"][0])):
        meta = results["metadatas"][0][i]
        items.append({
            "chunk_id": results["ids"][0][i],
            "text": results["documents"][0][i],
            "distance": results["distances"][0][i],
            "episode_guid": meta["episode_guid"],
            "episode_title": meta["episode_title"],
            "episode_date": meta["episode_date"],
            "episode_number": meta.get("episode_number"),
            "start_time": meta["start_time"] if meta["start_time"] != SENTINEL_NO_TIME else None,
            "end_time": meta["end_time"] if meta["end_time"] != SENTINEL_NO_TIME else None,
        })
    return items


def get_ingested_guids(collection: chromadb.Collection) -> set[str]:
    all_meta = collection.get(include=["metadatas"])
    guids = set()
    for meta in all_meta["metadatas"]:
        guids.add(meta["episode_guid"])
    return guids


def delete_episode(collection: chromadb.Collection, guid: str) -> None:
    collection.delete(where={"episode_guid": guid})


def export_episodes(
    collection: chromadb.Collection,
    episode_numbers: list[int] | None = None,
) -> list[dict]:
    """Export chunks with embeddings for the given episodes (or all if None)."""
    results = collection.get(include=["embeddings", "documents", "metadatas"])
    items = []
    for i, meta in enumerate(results["metadatas"]):
        if episode_numbers and meta.get("episode_number") not in episode_numbers:
            continue
        embedding = results["embeddings"][i]
        if hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
        items.append({
            "id": results["ids"][i],
            "document": results["documents"][i],
            "embedding": embedding,
            "metadata": meta,
        })
    return items


def import_chunks(
    collection: chromadb.Collection,
    items: list[dict],
    batch_size: int = 500,
) -> int:
    """Import exported chunks via upsert. Returns count imported."""
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        collection.upsert(
            ids=[it["id"] for it in batch],
            embeddings=[it["embedding"] for it in batch],
            documents=[it["document"] for it in batch],
            metadatas=[it["metadata"] for it in batch],
        )
    return len(items)


def _chunk_metadata(chunk: Chunk) -> dict:
    return {
        "episode_guid": chunk.episode_guid,
        "episode_title": chunk.episode_title,
        "episode_date": chunk.episode_date,
        "episode_number": chunk.episode_number or 0,
        "start_time": chunk.start_time if chunk.start_time is not None else SENTINEL_NO_TIME,
        "end_time": chunk.end_time if chunk.end_time is not None else SENTINEL_NO_TIME,
    }
