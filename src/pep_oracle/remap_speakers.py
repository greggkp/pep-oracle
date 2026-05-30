"""Re-process diarized episodes through the current speaker-mapping logic,
in place, without re-embedding.

Chunk embeddings derive from the transcript text, not the speaker labels, so we
rebuild each episode's chunks from the cached transcript + cached diarization
(re-diarizing only if the cache is missing) and reuse the stored embeddings,
matched by deterministic chunk_id. This applies the substantive-speaker mapping
(top clusters -> Chas/Dave/guest, tail -> skipped) to data that was ingested
before the fix existed, and is the same routine you'd run after tuning the
mapping. Idempotent.
"""

import logging
from datetime import datetime

from pep_oracle.chunking import chunk_transcript
from pep_oracle.feed import fetch_episodes
from pep_oracle.models import Episode
from pep_oracle.store import add_chunks, delete_episode
from pep_oracle.transcripts.diarize import (
    apply_diarization,
    get_speaker_segments,
    host_roster_from_title,
)
from pep_oracle.transcripts.manager import get_transcript

logger = logging.getLogger(__name__)


def diarized_guids(collection) -> set[str]:
    """GUIDs of episodes that carry diarization metadata in the collection."""
    got = collection.get(include=["metadatas"])
    return {m["episode_guid"] for m in got["metadatas"] if "speakers" in m}


def _episode_from_metadata(collection, guid: str) -> Episode | None:
    """Reconstruct a minimal Episode from stored chunk metadata for episodes no
    longer in the RSS feed window. Safe only because get_transcript /
    get_speaker_segments resolve from per-guid caches (no audio_url needed)."""
    got = collection.get(where={"episode_guid": guid}, include=["metadatas"], limit=1)
    if not got["metadatas"]:
        return None
    m = got["metadatas"][0]
    return Episode(
        guid=guid, title=m.get("episode_title", ""),
        pub_date=datetime.fromisoformat(m["episode_date"]),
        audio_url="", description="",
        episode_number=m.get("episode_number") or None,
    )


def reprocess_episode(collection, episode) -> dict:
    """Rebuild one episode's chunks with current speaker mapping, reusing the
    stored embeddings. Returns a small summary dict."""
    segments, _ = get_transcript(episode)  # cached
    speaker_segments = get_speaker_segments(episode.audio_url, episode.guid)  # cached
    roster = host_roster_from_title(episode.title)
    named = apply_diarization(segments, speaker_segments, roster=roster)
    chunks = chunk_transcript(named, episode)

    existing = collection.get(where={"episode_guid": episode.guid}, include=["embeddings"])
    emb_by_id = dict(zip(existing["ids"], existing["embeddings"]))
    missing = [c.chunk_id for c in chunks if c.chunk_id not in emb_by_id]
    if missing:
        raise RuntimeError(
            f"{episode.guid}: {len(missing)} chunk(s) have no stored embedding "
            f"(chunking changed?); aborting to avoid data loss: {missing[:3]}"
        )
    embeddings = [list(emb_by_id[c.chunk_id]) for c in chunks]

    speakers = sorted({s.speaker for s in named if s.speaker})
    delete_episode(collection, episode.guid)
    add_chunks(collection, chunks, embeddings)
    return {"title": episode.title, "chunks": len(chunks), "speakers": speakers}


def reprocess_diarized_episodes(collection) -> dict:
    """Re-process every diarized episode in the collection. Returns
    {guid: summary}."""
    guids = diarized_guids(collection)
    episodes = {e.guid: e for e in fetch_episodes()}
    summary: dict[str, dict] = {}
    for guid in guids:
        episode = episodes.get(guid) or _episode_from_metadata(collection, guid)
        if episode is None:
            logger.warning("diarized guid %s has no feed entry or metadata; skipping", guid)
            continue
        try:
            summary[guid] = reprocess_episode(collection, episode)
        except FileNotFoundError:
            # Out-of-feed episode whose caches were evicted — can't re-derive.
            logger.warning("diarized guid %s missing caches; skipping", guid)
    return summary
