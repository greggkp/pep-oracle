from datetime import datetime

from pep_oracle import remap_speakers
from pep_oracle.chunking import chunk_transcript
from pep_oracle.models import Episode, TranscriptSegment
from pep_oracle.remap_speakers import reprocess_episode
from pep_oracle.store import add_chunks, get_client
from pep_oracle.transcripts.diarize import SpeakerSegment


_counter = 0


def _fresh_collection():
    global _counter
    _counter += 1
    client = get_client(persistent=False)
    return client.get_or_create_collection(
        name=f"test_reproc_{_counter}", metadata={"hnsw:space": "cosine"}
    )


def _episode(title):
    return Episode(
        guid="g1", title=title, pub_date=datetime(2026, 5, 29),
        audio_url="https://x/ep.mp3", description="", episode_number=263,
    )


def _patch_caches(monkeypatch, transcript, speaker_segments):
    monkeypatch.setattr(remap_speakers, "get_transcript", lambda ep: (transcript, "cached"))
    monkeypatch.setattr(
        remap_speakers, "get_speaker_segments",
        lambda audio_url, guid: speaker_segments,
    )


def _seed_existing(col, episode, transcript):
    """Pre-seed the collection with this episode's chunks (any speaker state) so
    reprocess_episode can reuse the embeddings by chunk_id."""
    chunks = chunk_transcript(transcript, episode)
    add_chunks(col, chunks, [[1.0] + [0.0] * 9 for _ in chunks])
    return chunks


def test_reprocess_maps_dominant_to_chas_skips_tail(monkeypatch):
    col = _fresh_collection()
    episode = _episode("PEP with Chas & Dr Dave (Ep 263)")
    transcript = [
        TranscriptSegment(text="chas opening", start_time=0.0, end_time=50.0),
        TranscriptSegment(text="more chas", start_time=50.0, end_time=100.0),
        TranscriptSegment(text="lachie aside", start_time=100.0, end_time=103.0),
    ]
    # SPEAKER_00 dominates (97%); SPEAKER_01 is a 3% tail (Lachie) -> skipped.
    speaker_segments = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=100.0),
        SpeakerSegment(speaker="SPEAKER_01", start=100.0, end=103.0),
    ]
    _seed_existing(col, episode, transcript)
    _patch_caches(monkeypatch, transcript, speaker_segments)

    summary = reprocess_episode(col, episode)
    assert summary["speakers"] == ["Chas"]  # tail skipped, no Dave

    meta = col.get(include=["metadatas"])["metadatas"][0]
    assert meta.get("has_speaker_chas") is True
    assert "has_speaker_dave" not in meta
    assert not any(k.startswith("has_speaker_speaker_") for k in meta)
    assert "[Chas]" in meta["speaker_text"]
    # The 3% tail's text stays in the chunk body but carries no speaker label.
    assert "lachie aside" in col.get(include=["documents"])["documents"][0]


def test_reprocess_two_substantive_hosts(monkeypatch):
    col = _fresh_collection()
    episode = _episode("PEP with Chas & Dr Dave (Ep 262)")
    transcript = [
        TranscriptSegment(text="chas part", start_time=0.0, end_time=60.0),
        TranscriptSegment(text="dave part", start_time=60.0, end_time=100.0),
    ]
    # 60/40 split -> both substantive -> Chas + Dave.
    speaker_segments = [
        SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=60.0),
        SpeakerSegment(speaker="SPEAKER_01", start=60.0, end=100.0),
    ]
    _seed_existing(col, episode, transcript)
    _patch_caches(monkeypatch, transcript, speaker_segments)

    summary = reprocess_episode(col, episode)
    assert summary["speakers"] == ["Chas", "Dave"]
    meta = col.get(include=["metadatas"])["metadatas"][0]
    assert meta.get("has_speaker_chas") is True
    assert meta.get("has_speaker_dave") is True


def test_reprocess_reuses_embeddings(monkeypatch):
    col = _fresh_collection()
    episode = _episode("PEP with Chas & Dr Dave (Ep 263)")
    transcript = [TranscriptSegment(text="hello", start_time=0.0, end_time=10.0)]
    speaker_segments = [SpeakerSegment(speaker="SPEAKER_00", start=0.0, end=10.0)]
    _seed_existing(col, episode, transcript)
    _patch_caches(monkeypatch, transcript, speaker_segments)

    sentinel = [0.5] + [0.1] * 9
    col.update(ids=["g1_0000"], embeddings=[sentinel])  # mark the stored vector
    reprocess_episode(col, episode)
    got = col.get(include=["embeddings"])
    assert [round(x, 4) for x in got["embeddings"][0]] == [round(x, 4) for x in sentinel]
