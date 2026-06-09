"""Tests for ingest.episode_chunks_and_embeddings (the Fargate-path core)."""

import time
from datetime import datetime, timezone
from unittest.mock import patch

from pep_oracle import ingest
from pep_oracle.models import Chunk, Episode, TranscriptSegment
from pep_oracle.transcripts.diarize import SpeakerSegment


def _ep(num: int = 300, guid: str | None = None) -> Episode:
    return Episode(
        guid=guid or f"guid-{num}",
        title=f"Test Episode (Ep {num}, 1 Jan)",
        pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        audio_url=f"https://example.com/ep{num}.mp3",
        description=f"Episode {num}",
        duration_seconds=9000,
        episode_number=num,
    )


FAKE_SEGMENTS = [
    TranscriptSegment(text="Hello world", start_time=0.0, end_time=10.0),
    TranscriptSegment(text="More content here for chunking", start_time=10.0, end_time=20.0),
]


def _fake_embed(texts):
    return [[0.1] * 1024 for _ in texts]


def test_episode_chunks_and_embeddings_returns_chunks_and_vectors(monkeypatch):
    """Returns (chunks, embeddings) with correct shapes and episode linkage."""
    monkeypatch.setattr(
        ingest, "_run_transcribe_and_diarize",
        lambda episode, diarize, cb: (
            [TranscriptSegment(text="hello world", start_time=0.0, end_time=5.0)],
            "whisper", [], 0.0, 0.0,
        ),
    )
    monkeypatch.setattr(ingest, "embed_texts", lambda texts: [[0.1] * 1024 for _ in texts])

    ep = _ep(300)
    chunks, embeddings = ingest.episode_chunks_and_embeddings(ep, diarize=False)

    assert chunks, "should return at least one chunk"
    assert all(isinstance(c, Chunk) for c in chunks)
    assert len(embeddings) == len(chunks)
    assert all(len(v) == 1024 for v in embeddings)
    assert chunks[0].episode_guid == "guid-300"


def test_episode_chunks_and_embeddings_returns_empty_on_no_segments(monkeypatch):
    """When transcription yields no segments, returns ([], []) without calling embed."""
    embed_called = []
    monkeypatch.setattr(
        ingest, "_run_transcribe_and_diarize",
        lambda episode, diarize, cb: ([], "whisper", None, 0.0, 0.0),
    )
    monkeypatch.setattr(ingest, "embed_texts", lambda texts: embed_called.append(texts) or [])

    ep = _ep(301)
    chunks, embeddings = ingest.episode_chunks_and_embeddings(ep, diarize=False)

    assert chunks == []
    assert embeddings == []
    assert not embed_called, "embed_texts should not be called when there are no chunks"


def test_episode_chunks_and_embeddings_one_embedding_per_chunk(monkeypatch):
    """The number of embeddings must exactly match the number of chunks."""
    segments = FAKE_SEGMENTS * 3  # enough to guarantee multiple chunks
    monkeypatch.setattr(
        ingest, "_run_transcribe_and_diarize",
        lambda episode, diarize, cb: (segments, "whisper", None, 0.0, 0.0),
    )
    monkeypatch.setattr(ingest, "embed_texts", _fake_embed)

    ep = _ep(302)
    chunks, embeddings = ingest.episode_chunks_and_embeddings(ep, diarize=False)

    assert len(chunks) == len(embeddings)
    assert len(chunks) > 0


def test_episode_chunks_and_embeddings_chunk_metadata_matches_episode(monkeypatch):
    """Chunks carry the episode's title and pub_date."""
    monkeypatch.setattr(
        ingest, "_run_transcribe_and_diarize",
        lambda episode, diarize, cb: (FAKE_SEGMENTS, "whisper", None, 0.0, 0.0),
    )
    monkeypatch.setattr(ingest, "embed_texts", _fake_embed)

    ep = _ep(303)
    chunks, _ = ingest.episode_chunks_and_embeddings(ep, diarize=False)

    assert all(c.episode_guid == ep.guid for c in chunks)
    assert all(c.episode_number == 303 for c in chunks)


def test_run_transcribe_and_diarize_runs_concurrently():
    """When diarize=True, transcription and diarization run in parallel.
    Wall-clock should be ~max(t1, t2), not t1 + t2."""
    slow_segments = FAKE_SEGMENTS
    slow_speakers = [SpeakerSegment(speaker="S1", start=0.0, end=20.0)]

    def slow_transcript(ep, progress_callback=None):
        time.sleep(0.3)
        return slow_segments, "whisper"

    def slow_speaker_segments(audio_url, episode_guid, num_speakers=None, progress_callback=None):
        time.sleep(0.3)
        return slow_speakers

    with (
        patch("pep_oracle.ingest.get_transcript", side_effect=slow_transcript),
        patch(
            "pep_oracle.transcripts.diarize.get_speaker_segments",
            side_effect=slow_speaker_segments,
        ),
    ):
        ep = _ep(304)
        start = time.monotonic()
        segments, source, speaker_segs, t_el, d_el = ingest._run_transcribe_and_diarize(
            ep, diarize_enabled=True, progress_callback=None
        )
        elapsed = time.monotonic() - start

    # Sequential would be ~0.6s; parallel should be ~0.3s. Allow 0.5s as the cap.
    assert elapsed < 0.5, f"expected parallel execution (<0.5s), got {elapsed:.2f}s"
    assert segments == slow_segments
    assert speaker_segs == slow_speakers
