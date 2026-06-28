"""Tests for the ChromaDB-free survivors of store.py.

store.py no longer imports chromadb. The query/upsert/export/import/where-clause
helpers (and their tests) were removed with the ChromaDB serving path; retrieval
is now tested against the InMemoryCorpus artifact (tests/test_hybrid.py,
tests/test_corpus.py). What remains here:
  - SENTINEL_NO_TIME (the no-timestamp sentinel, shared with hybrid.py)
  - _chunk_metadata (the canonical chunk->metadata builder, incl. speaker fields)
  - get_ingestion_stats (run against an InMemoryCorpus, the prod serving type)
"""

import json

import pep_oracle.corpus as corpus
from pep_oracle.models import Chunk
from pep_oracle.store import SENTINEL_NO_TIME, _chunk_metadata, get_ingestion_stats

_counter = 0


def _row(chunk, embedding):
    return {
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "embedding": embedding,
        "metadata": _chunk_metadata(chunk),
    }


def _corpus_from_chunks(tmp_path, chunks_and_embs):
    """Build an InMemoryCorpus (the prod serving type) from (Chunk, embedding) pairs."""
    global _counter
    _counter += 1
    rows = [_row(c, e) for c, e in chunks_and_embs]
    dest = tmp_path / f"store{_counter}"
    dims = len(chunks_and_embs[0][1]) if chunks_and_embs else 10
    corpus.write_artifact(
        rows,
        dest=str(dest),
        version="v0001",
        embed_model="m",
        dims=dims,
        git_sha="s",
        built_at="t",
    )
    return corpus.load_current(str(dest))


# --- SENTINEL_NO_TIME ---


def test_sentinel_no_time_value():
    assert SENTINEL_NO_TIME == -1.0


# --- _chunk_metadata (no chromadb; pure Chunk -> dict) ---


def test_chunk_metadata_basic_fields():
    chunk = Chunk(
        chunk_id="ep-1_0000",
        episode_guid="ep-1",
        text="hello",
        episode_title="Episode 1",
        episode_date="2026-01-01",
        episode_number=251,
        start_time=12.5,
        end_time=240.0,
    )
    meta = _chunk_metadata(chunk)
    assert meta == {
        "episode_guid": "ep-1",
        "episode_title": "Episode 1",
        "episode_date": "2026-01-01",
        "episode_number": 251,
        "start_time": 12.5,
        "end_time": 240.0,
    }


def test_chunk_metadata_missing_times_use_sentinel():
    chunk = Chunk(
        chunk_id="ep-1_0000",
        episode_guid="ep-1",
        text="No timing info",
        episode_title="Episode",
        episode_date="2026-01-01",
    )
    meta = _chunk_metadata(chunk)
    assert meta["start_time"] == SENTINEL_NO_TIME
    assert meta["end_time"] == SENTINEL_NO_TIME
    # episode_number defaults to 0 (the "no episode" sentinel) when unset.
    assert meta["episode_number"] == 0


def test_chunk_metadata_speaker_fields():
    chunk = Chunk(
        chunk_id="ep-1_0000",
        episode_guid="ep-1",
        text="I think so. Me too.",
        episode_title="Episode 1",
        episode_date="2026-01-01",
        episode_number=1,
        start_time=0.0,
        end_time=10.0,
        speaker_text="[Chas] I think so. [Dave] Me too.",
        speaker_turns=[
            {"speaker": "Chas", "start": 0.0, "end": 5.0},
            {"speaker": "Dave", "start": 5.0, "end": 10.0},
        ],
    )
    meta = _chunk_metadata(chunk)
    assert meta["speaker_text"] == "[Chas] I think so. [Dave] Me too."
    # Boolean per-speaker flags enable speaker filtering.
    assert meta["has_speaker_chas"] is True
    assert meta["has_speaker_dave"] is True
    # Turn boundaries serialized for query-time hybrid trim.
    assert json.loads(meta["speakers"]) == chunk.speaker_turns
    # The old comma-string field must not resurface.
    assert "speaker_list" not in meta


def test_chunk_metadata_no_speaker_fields_when_absent():
    chunk = Chunk(
        chunk_id="ep-1_0000",
        episode_guid="ep-1",
        text="plain",
        episode_title="Episode 1",
        episode_date="2026-01-01",
        episode_number=1,
    )
    meta = _chunk_metadata(chunk)
    assert "speaker_text" not in meta
    assert "speakers" not in meta
    assert not any(k.startswith("has_speaker_") for k in meta)


def test_speaker_name_with_space_normalized():
    chunk = Chunk(
        chunk_id="ep-1_0000",
        episode_guid="ep-1",
        text="x",
        episode_title="Episode 1",
        episode_date="2026-01-01",
        episode_number=1,
        speaker_turns=[{"speaker": "Special Guest", "start": 0.0, "end": 5.0}],
    )
    meta = _chunk_metadata(chunk)
    assert meta["has_speaker_special_guest"] is True


# --- get_ingestion_stats (runs against the InMemoryCorpus serving type) ---


def _dated_chunk(guid, ep_num, date):
    chunk = Chunk(
        chunk_id=f"{guid}_0000",
        episode_guid=guid,
        text=f"Content from episode {ep_num}",
        episode_title=f"Episode {ep_num}",
        episode_date=date,
        episode_number=ep_num,
        start_time=0.0,
        end_time=60.0,
    )
    return chunk, [1.0] + [0.0] * 9


def test_get_ingestion_stats(tmp_path):
    data = [
        ("ep-220", 220, "2024-06-15"),
        ("ep-240", 240, "2025-11-15"),
        ("ep-248", 248, "2026-03-06"),
        ("ep-251", 251, "2026-03-21"),
    ]
    col = _corpus_from_chunks(tmp_path, [_dated_chunk(g, n, d) for g, n, d in data])
    stats = get_ingestion_stats(col)
    assert stats["earliest_date"] == "2024-06-15"
    assert stats["latest_date"] == "2026-03-21"
    assert stats["earliest_episode"] == 220
    assert stats["latest_episode"] == 251


def test_get_ingestion_stats_empty(tmp_path):
    corpus.write_artifact(
        [],
        dest=str(tmp_path),
        version="v0001",
        embed_model="m",
        dims=10,
        git_sha="s",
        built_at="t",
    )
    col = corpus.load_current(str(tmp_path))
    stats = get_ingestion_stats(col)
    assert stats["earliest_date"] is None
    assert stats["latest_date"] is None
    assert stats["earliest_episode"] is None
    assert stats["latest_episode"] is None
