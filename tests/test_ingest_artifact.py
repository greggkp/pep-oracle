"""Tests for the artifact-native incremental ingest (ingest_artifact.py)."""

from __future__ import annotations

from datetime import UTC, datetime

from pep_oracle import ingest_artifact
from pep_oracle.corpus import InMemoryCorpus
from pep_oracle.models import Chunk, Episode


def _ep(guid, num):
    return Episode(
        guid=guid,
        title=f"T (Ep {num}, 1 Jan)",
        pub_date=datetime(2026, 1, 1, tzinfo=UTC),
        audio_url=f"http://x/{guid}.mp3",
        description="d",
        episode_number=num,
    )


def _ep_unnumbered(guid):
    # an "EXTRA" bonus episode whose title the episode-number regex can't parse
    return Episode(
        guid=guid,
        title="PEP250 CORRESPONDENCE EXTRA",
        pub_date=datetime(2026, 1, 2, tzinfo=UTC),
        audio_url=f"http://x/{guid}.mp3",
        description="d",
        episode_number=None,
    )


def _fake_write(captured):
    from pep_oracle.corpus import Manifest

    def write(rows, *, dest, version, embed_model, dims, git_sha, built_at):
        captured.update(rows=rows, version=version, dims=dims)
        nums = sorted(
            r["metadata"].get("episode_number") for r in rows if r["metadata"].get("episode_number")
        )
        rng = [nums[0], nums[-1]] if nums else [None, None]
        return Manifest(1, embed_model, dims, rng, len(rows), git_sha, built_at, "sha")

    return write


def _existing_corpus():
    # one already-ingested episode (guid g1), one chunk
    metas = [
        {
            "episode_guid": "g1",
            "episode_title": "T (Ep 250)",
            "episode_date": "2026-01-01",
            "episode_number": 250,
            "start_time": 0.0,
            "end_time": 5.0,
        }
    ]
    return InMemoryCorpus(
        ids=["g1_0000"], docs=["old text"], embeddings=[[0.0] * 1024], metas=metas, version="v0001"
    )


def _new_chunk(guid, num):
    return Chunk(
        chunk_id=f"{guid}_0000",
        episode_guid=guid,
        text="new text",
        episode_title=f"T (Ep {num})",
        episode_date=f"2026-01-0{num}",
        start_time=0.0,
        end_time=5.0,
        episode_number=num,
    )


def _patch_common(monkeypatch, feed_eps, new_guids_processed):
    monkeypatch.setattr(ingest_artifact, "load_current", lambda base: _existing_corpus())
    monkeypatch.setattr(ingest_artifact, "fetch_episodes", lambda: feed_eps)
    monkeypatch.setattr(ingest_artifact, "_download_profiles", lambda dest: None)  # no S3 in tests

    def fake_proc(episode, *, diarize, profile_path, progress_callback=None):
        new_guids_processed.append(episode.guid)
        return [_new_chunk(episode.guid, episode.episode_number)], [[0.2] * 1024]

    monkeypatch.setattr(ingest_artifact, "episode_chunks_and_embeddings", fake_proc)


def test_incremental_processes_only_new_and_merges(monkeypatch):
    processed = []
    _patch_common(monkeypatch, [_ep("g1", 250), _ep("g2", 251)], processed)
    captured = {}

    def fake_write(rows, *, dest, version, embed_model, dims, git_sha, built_at):
        captured.update(rows=rows, version=version, dims=dims)
        from pep_oracle.corpus import Manifest

        return Manifest(1, embed_model, dims, [250, 251], len(rows), git_sha, built_at, "sha")

    monkeypatch.setattr(ingest_artifact, "write_artifact", fake_write)

    manifest = ingest_artifact.ingest_artifact_incremental(dest="s3://b", diarize=True)
    assert processed == ["g2"]  # only the new episode
    assert captured["version"] == "v0002"  # incremented
    ids = [r["chunk_id"] for r in captured["rows"]]
    assert ids == ["g1_0000", "g2_0000"]  # existing + new, merged
    assert captured["rows"][1]["metadata"]["episode_guid"] == "g2"
    assert len(captured["rows"][1]["embedding"]) == 1024
    assert manifest.chunk_count == 2


def test_no_new_episodes_is_noop(monkeypatch):
    processed = []
    _patch_common(monkeypatch, [_ep("g1", 250)], processed)  # feed == already-ingested
    called = {"wrote": False}
    monkeypatch.setattr(
        ingest_artifact, "write_artifact", lambda *a, **k: called.update(wrote=True)
    )
    result = ingest_artifact.ingest_artifact_incremental(dest="s3://b", diarize=True)
    assert result is None
    assert called["wrote"] is False
    assert processed == []


def test_newest_forward_skips_old_gap_and_unnumbered(monkeypatch):
    """Default mode: only numbered episodes NEWER than the corpus max (250). An old
    missing episode (240, a back-catalogue gap) and an unnumbered EXTRA are left out —
    so one permanent gap can't make every run a fragile all-or-nothing backfill."""
    processed = []
    feed = [_ep("g_old", 240), _ep("g1", 250), _ep("g2", 251), _ep_unnumbered("g_extra")]
    _patch_common(monkeypatch, feed, processed)
    captured = {}
    monkeypatch.setattr(ingest_artifact, "write_artifact", _fake_write(captured))

    ingest_artifact.ingest_artifact_incremental(dest="s3://b", diarize=True)
    assert processed == ["g2"]  # 240 (gap) + g_extra (unnumbered) skipped
    assert [r["chunk_id"] for r in captured["rows"]] == ["g1_0000", "g2_0000"]


def test_backfill_ingests_old_gap_and_unnumbered(monkeypatch):
    """backfill=True: every feed episode the corpus lacks — old gap (240) + the
    unnumbered EXTRA — but never the already-present g1/250."""
    processed = []
    feed = [_ep("g_old", 240), _ep("g1", 250), _ep("g2", 251), _ep_unnumbered("g_extra")]
    _patch_common(monkeypatch, feed, processed)
    captured = {}
    monkeypatch.setattr(ingest_artifact, "write_artifact", _fake_write(captured))

    ingest_artifact.ingest_artifact_incremental(dest="s3://b", diarize=True, backfill=True)
    assert sorted(processed) == ["g2", "g_extra", "g_old"]  # all missing; g1 already present
