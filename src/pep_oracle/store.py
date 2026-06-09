"""ChromaDB-free chunk-metadata + corpus-stats helpers.

ChromaDB was removed from the serving/ingest path (the corpus is a versioned
parquet artifact loaded as an InMemoryCorpus; see corpus.py). What remains here
is generic and storage-agnostic:
  - SENTINEL_NO_TIME: the no-timestamp sentinel, shared with hybrid.py
  - _chunk_metadata: the canonical Chunk -> metadata-dict builder (incl. speaker
    fields), used by the ingest path that builds corpus rows
  - get_ingestion_stats: summary over any object exposing the corpus read
    surface (`.get(include=["metadatas"])`) — the InMemoryCorpus serving type
"""

import json

from pep_oracle.models import Chunk

SENTINEL_NO_TIME = -1.0


def get_ingestion_stats(collection) -> dict:
    """Return summary stats about ingested episodes.

    ``collection`` is any object exposing ``.get(include=["metadatas"])`` —
    the InMemoryCorpus serving type satisfies this.
    """
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
        meta["speakers"] = json.dumps(chunk.speaker_turns)
        unique = {t["speaker"] for t in chunk.speaker_turns}
        for speaker in unique:
            key = f"has_speaker_{speaker.lower().replace(' ', '_')}"
            meta[key] = True
    return meta
