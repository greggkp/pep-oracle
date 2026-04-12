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
    episode_numbers: list[int] | None = None,
    after_date: str | None = None,
    before_date: str | None = None,
    recency_weight: float = 0.0,
) -> list[dict]:
    # ChromaDB where clause handles episode number filtering;
    # date filtering is done in Python since episode_date is a string.
    where = _build_where(
        episode_number=episode_number,
        episode_numbers=episode_numbers,
    )
    # Fetch extra results when we'll post-filter or re-rank
    needs_extra = after_date or before_date or recency_weight > 0
    fetch_k = top_k * 3 if needs_extra else top_k
    results = collection.query(
        query_embeddings=[embedding],
        n_results=fetch_k,
        where=where or None,
        include=["documents", "metadatas", "distances"],
    )
    items = []
    for i in range(len(results["ids"][0])):
        meta = results["metadatas"][0][i]
        ep_date = meta["episode_date"]
        # Date filtering (string comparison works for YYYY-MM-DD format)
        if after_date and ep_date < after_date:
            continue
        if before_date and ep_date > before_date:
            continue
        item = {
            "chunk_id": results["ids"][0][i],
            "text": results["documents"][0][i],
            "distance": results["distances"][0][i],
            "episode_guid": meta["episode_guid"],
            "episode_title": meta["episode_title"],
            "episode_date": ep_date,
            "episode_number": meta.get("episode_number"),
            "start_time": meta["start_time"] if meta["start_time"] != SENTINEL_NO_TIME else None,
            "end_time": meta["end_time"] if meta["end_time"] != SENTINEL_NO_TIME else None,
        }
        if "speaker_text" in meta:
            item["speaker_text"] = meta["speaker_text"]
        if "speaker_list" in meta:
            item["speaker_list"] = meta["speaker_list"]
        items.append(item)

    if recency_weight > 0 and items:
        items = _apply_recency_boost(items, recency_weight)

    return items[:top_k]


def _apply_recency_boost(items: list[dict], weight: float) -> list[dict]:
    """Re-rank items by blending similarity distance with recency score.

    Lower distance = more similar. We convert to a similarity score (1 - dist),
    blend with a 0-1 recency score, then sort descending by blended score.
    """
    dates = [it["episode_date"] for it in items]
    min_date, max_date = min(dates), max(dates)

    for it in items:
        # Similarity score: 1 - distance (higher = better)
        sim_score = 1.0 - it["distance"]

        # Recency score: 0 (oldest) to 1 (newest) in result set
        if min_date == max_date:
            recency_score = 1.0
        else:
            recency_score = (it["episode_date"] >= max_date) * 0.4
            # Finer: linear interpolation based on string sort position
            all_unique = sorted(set(dates))
            idx = all_unique.index(it["episode_date"])
            recency_score = idx / (len(all_unique) - 1) if len(all_unique) > 1 else 1.0

        it["_blended"] = sim_score * (1 - weight) + recency_score * weight

    items.sort(key=lambda it: it["_blended"], reverse=True)

    # Clean up temp key
    for it in items:
        del it["_blended"]

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


def get_ingestion_stats(collection: chromadb.Collection) -> dict:
    """Return summary stats about ingested episodes."""
    all_meta = collection.get(include=["metadatas"])
    if not all_meta["metadatas"]:
        return {
            "earliest_date": None,
            "latest_date": None,
            "earliest_episode": None,
            "latest_episode": None,
        }
    dates = set()
    episode_numbers = set()
    for meta in all_meta["metadatas"]:
        dates.add(meta["episode_date"])
        ep_num = meta.get("episode_number", 0)
        if ep_num:
            episode_numbers.add(ep_num)
    return {
        "earliest_date": min(dates) if dates else None,
        "latest_date": max(dates) if dates else None,
        "earliest_episode": min(episode_numbers) if episode_numbers else None,
        "latest_episode": max(episode_numbers) if episode_numbers else None,
    }


def _build_where(
    episode_number: int | None = None,
    episode_numbers: list[int] | None = None,
) -> dict | None:
    """Build a ChromaDB where clause for episode number filtering."""
    if episode_number:
        return {"episode_number": episode_number}
    if episode_numbers:
        if len(episode_numbers) == 1:
            return {"episode_number": episode_numbers[0]}
        return {"episode_number": {"$in": episode_numbers}}
    return None


def _chunk_metadata(chunk: Chunk) -> dict:
    meta = {
        "episode_guid": chunk.episode_guid,
        "episode_title": chunk.episode_title,
        "episode_date": chunk.episode_date,
        "episode_number": chunk.episode_number or 0,
        "start_time": chunk.start_time if chunk.start_time is not None else SENTINEL_NO_TIME,
        "end_time": chunk.end_time if chunk.end_time is not None else SENTINEL_NO_TIME,
    }
    if chunk.speaker_text is not None:
        meta["speaker_text"] = chunk.speaker_text
    if chunk.speaker_turns is not None:
        import json
        meta["speakers"] = json.dumps(chunk.speaker_turns)
        unique = sorted(set(t["speaker"] for t in chunk.speaker_turns))
        meta["speaker_list"] = ",".join(unique)
    return meta
