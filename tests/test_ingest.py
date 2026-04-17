from datetime import datetime, timezone
from unittest.mock import patch

import chromadb

from pep_oracle.ingest import estimate_whisper_cost, ingest_all, ingest_episode
from pep_oracle.models import Episode, TranscriptSegment
from pep_oracle.store import get_collection, get_ingested_guids


def _make_episode(num: int, guid: str | None = None, duration: int = 9000) -> Episode:
    return Episode(
        guid=guid or f"guid-{num}",
        title=f"TEST EPISODE (Ep {num}, 1 Jan)",
        pub_date=datetime(2026, 1, num, tzinfo=timezone.utc),
        audio_url=f"https://example.com/ep{num}.mp3",
        description=f"Episode {num}",
        duration_seconds=duration,
        episode_number=num,
    )


FAKE_SEGMENTS = [
    TranscriptSegment(text="Hello world", start_time=0.0, end_time=10.0),
    TranscriptSegment(text="More content here for chunking", start_time=10.0, end_time=20.0),
]

def _fake_embed(texts, **kwargs):
    """Return one embedding per input text, matching the real embed_texts signature."""
    return [[0.1] * 10 for _ in texts]

_counter = 0


def _fresh_collection():
    global _counter
    _counter += 1
    client = chromadb.Client()
    return client.get_or_create_collection(
        name=f"ingest_test_{_counter}",
        metadata={"hnsw:space": "cosine"},
    )


def test_estimate_whisper_cost():
    episodes = [_make_episode(1, duration=3600), _make_episode(2, duration=7200)]
    cost = estimate_whisper_cost(episodes)
    # 60 + 120 = 180 minutes × $0.001 = $0.18
    assert abs(cost - 0.18) < 0.01


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_all_stores_chunks_in_collection(mock_embed, mock_transcript, mock_fetch):
    """After ingesting, chunks should exist in ChromaDB for each episode."""
    collection = _fresh_collection()
    mock_fetch.return_value = [_make_episode(1), _make_episode(2)]

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value=set()),
    ):
        result = ingest_all(confirm_cost=False)

    assert result["processed"] == 2
    assert result["failed"] == 0
    # Verify actual data in ChromaDB
    guids = get_ingested_guids(collection)
    assert guids == {"guid-1", "guid-2"}
    assert collection.count() > 0


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_all_skips_already_ingested(mock_embed, mock_transcript, mock_fetch):
    """Episodes whose GUIDs are already present should be skipped."""
    collection = _fresh_collection()
    mock_fetch.return_value = [_make_episode(1), _make_episode(2)]

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value={"guid-1"}),
    ):
        result = ingest_all(confirm_cost=False)

    assert result["processed"] == 1
    assert result["skipped"] == 1
    # Only episode 2's transcript should have been fetched
    assert mock_transcript.call_count == 1
    call_ep = mock_transcript.call_args[0][0]
    assert call_ep.episode_number == 2


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_all_force_replaces_existing(mock_embed, mock_transcript, mock_fetch):
    """With force=True, previously ingested episodes should be re-ingested."""
    collection = _fresh_collection()
    mock_fetch.return_value = [_make_episode(1), _make_episode(2)]

    # Pre-populate collection with guid-1 data so delete has something to remove
    from pep_oracle.store import add_chunks
    from pep_oracle.models import Chunk

    old_chunk = Chunk(
        chunk_id="guid-1_old",
        episode_guid="guid-1",
        text="Old data",
        episode_title="Old",
        episode_date="2026-01-01",
        episode_number=1,
        start_time=0.0,
        end_time=10.0,
    )
    add_chunks(collection, [old_chunk], [[0.5] * 10])

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value={"guid-1"}),
    ):
        result = ingest_all(force=True, confirm_cost=False)

    assert result["processed"] == 2
    guids = get_ingested_guids(collection)
    assert guids == {"guid-1", "guid-2"}
    # Old chunk should have been replaced — "Old data" should not appear
    all_docs = collection.get(include=["documents"])
    assert "Old data" not in all_docs["documents"]


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_transcript", side_effect=[Exception("Whisper failed"), (FAKE_SEGMENTS, "whisper_cached")])
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_all_continues_on_failure(mock_embed, mock_transcript, mock_fetch):
    """A failure on one episode should not prevent processing the rest."""
    collection = _fresh_collection()
    mock_fetch.return_value = [_make_episode(1), _make_episode(2)]

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value=set()),
    ):
        result = ingest_all(confirm_cost=False)

    assert result["processed"] == 1
    assert result["failed"] == 1
    # Only episode 2 should be in the collection (episode 1 failed)
    guids = get_ingested_guids(collection)
    assert len(guids) == 1


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_episode_by_number(mock_embed, mock_transcript, mock_fetch):
    """Ingesting by episode number should only process that episode."""
    collection = _fresh_collection()
    mock_fetch.return_value = [_make_episode(1), _make_episode(2), _make_episode(3)]

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value=set()),
    ):
        result = ingest_episode("2")

    assert result is True
    guids = get_ingested_guids(collection)
    assert guids == {"guid-2"}


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_all_filters_by_episode_numbers(mock_embed, mock_transcript, mock_fetch):
    """When episode_numbers is provided, only those episodes are processed."""
    collection = _fresh_collection()
    mock_fetch.return_value = [_make_episode(1), _make_episode(2), _make_episode(3)]

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value=set()),
    ):
        result = ingest_all(confirm_cost=False, episode_numbers=[2, 3])

    assert result["processed"] == 2
    assert result["failed"] == 0
    guids = get_ingested_guids(collection)
    assert guids == {"guid-2", "guid-3"}
    call_eps = [call[0][0].episode_number for call in mock_transcript.call_args_list]
    assert 1 not in call_eps


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_all_new_only_skips_historical_gaps(mock_embed, mock_transcript, mock_fetch):
    """new_only should only process episodes with pub_date > latest ingested, ignoring older gaps."""
    collection = _fresh_collection()
    # Episodes 1, 2, 3, 4 (pub_dates Jan 1-4). Say ep 2 is already ingested.
    # new_only should process only episodes 3 and 4 (newer than ep 2), skipping gap at ep 1.
    mock_fetch.return_value = [_make_episode(1), _make_episode(2), _make_episode(3), _make_episode(4)]

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value={"guid-2"}),
    ):
        result = ingest_all(confirm_cost=False, new_only=True)

    assert result["processed"] == 2
    guids = get_ingested_guids(collection)
    assert guids == {"guid-3", "guid-4"}


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_all_new_only_no_baseline_skips(mock_embed, mock_transcript, mock_fetch):
    """new_only with nothing ingested yet should do nothing (no baseline to compare against)."""
    collection = _fresh_collection()
    mock_fetch.return_value = [_make_episode(1), _make_episode(2)]

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value=set()),
    ):
        result = ingest_all(confirm_cost=False, new_only=True)

    assert result["processed"] == 0
    assert mock_transcript.call_count == 0


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_all_calls_progress_callback(mock_embed, mock_transcript, mock_fetch):
    """progress_callback should be called with episode and step info."""
    collection = _fresh_collection()
    mock_fetch.return_value = [_make_episode(1)]
    calls = []

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value=set()),
    ):
        result = ingest_all(confirm_cost=False, progress_callback=calls.append)

    assert result["processed"] == 1
    assert any("Ep 1" in c for c in calls)
    assert any("embedding" in c.lower() or "storing" in c.lower() for c in calls)
