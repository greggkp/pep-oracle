from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from pep_oracle.ingest import estimate_whisper_cost, ingest_all, ingest_episode
from pep_oracle.models import Chunk, Episode, TranscriptSegment


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

FAKE_EMBEDDINGS = [[0.1] * 10, [0.2] * 10]


def test_estimate_whisper_cost():
    episodes = [_make_episode(1, duration=3600), _make_episode(2, duration=7200)]
    cost = estimate_whisper_cost(episodes)
    # 60 + 120 = 180 minutes × $0.006 = $1.08
    assert abs(cost - 1.08) < 0.01


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_client")
@patch("pep_oracle.ingest.get_collection")
@patch("pep_oracle.ingest.get_ingested_guids", return_value=set())
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", return_value=FAKE_EMBEDDINGS)
@patch("pep_oracle.ingest.add_chunks")
@patch("pep_oracle.ingest.delete_episode")
def test_ingest_all_processes_new_episodes(
    mock_delete, mock_add, mock_embed, mock_transcript,
    mock_guids, mock_col, mock_client, mock_fetch,
):
    mock_fetch.return_value = [_make_episode(1), _make_episode(2)]

    result = ingest_all(confirm_cost=False)

    assert result["processed"] == 2
    assert result["failed"] == 0
    assert mock_transcript.call_count == 2
    assert mock_embed.call_count == 2
    assert mock_add.call_count == 2


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_client")
@patch("pep_oracle.ingest.get_collection")
@patch("pep_oracle.ingest.get_ingested_guids", return_value={"guid-1"})
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", return_value=FAKE_EMBEDDINGS)
@patch("pep_oracle.ingest.add_chunks")
@patch("pep_oracle.ingest.delete_episode")
def test_ingest_all_skips_already_ingested(
    mock_delete, mock_add, mock_embed, mock_transcript,
    mock_guids, mock_col, mock_client, mock_fetch,
):
    mock_fetch.return_value = [_make_episode(1), _make_episode(2)]

    result = ingest_all(confirm_cost=False)

    assert result["processed"] == 1
    assert result["skipped"] == 1
    # Only episode 2 should be processed
    mock_transcript.assert_called_once()
    call_ep = mock_transcript.call_args[0][0]
    assert call_ep.episode_number == 2


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_client")
@patch("pep_oracle.ingest.get_collection")
@patch("pep_oracle.ingest.get_ingested_guids", return_value={"guid-1"})
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", return_value=FAKE_EMBEDDINGS)
@patch("pep_oracle.ingest.add_chunks")
@patch("pep_oracle.ingest.delete_episode")
def test_ingest_all_force_reprocesses(
    mock_delete, mock_add, mock_embed, mock_transcript,
    mock_guids, mock_col, mock_client, mock_fetch,
):
    mock_fetch.return_value = [_make_episode(1), _make_episode(2)]

    result = ingest_all(force=True, confirm_cost=False)

    assert result["processed"] == 2
    assert mock_delete.call_count == 2  # both episodes deleted before re-ingest


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_client")
@patch("pep_oracle.ingest.get_collection")
@patch("pep_oracle.ingest.get_ingested_guids", return_value=set())
@patch("pep_oracle.ingest.get_transcript", side_effect=[Exception("Whisper failed"), (FAKE_SEGMENTS, "whisper_cached")])
@patch("pep_oracle.ingest.embed_texts", return_value=FAKE_EMBEDDINGS)
@patch("pep_oracle.ingest.add_chunks")
@patch("pep_oracle.ingest.delete_episode")
def test_ingest_all_continues_on_failure(
    mock_delete, mock_add, mock_embed, mock_transcript,
    mock_guids, mock_col, mock_client, mock_fetch,
):
    mock_fetch.return_value = [_make_episode(1), _make_episode(2)]

    result = ingest_all(confirm_cost=False)

    assert result["processed"] == 1
    assert result["failed"] == 1


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_client")
@patch("pep_oracle.ingest.get_collection")
@patch("pep_oracle.ingest.get_ingested_guids", return_value=set())
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", return_value=FAKE_EMBEDDINGS)
@patch("pep_oracle.ingest.add_chunks")
@patch("pep_oracle.ingest.delete_episode")
def test_ingest_episode_by_number(
    mock_delete, mock_add, mock_embed, mock_transcript,
    mock_guids, mock_col, mock_client, mock_fetch,
):
    mock_fetch.return_value = [_make_episode(1), _make_episode(2), _make_episode(3)]

    result = ingest_episode("2")

    assert result is True
    call_ep = mock_transcript.call_args[0][0]
    assert call_ep.episode_number == 2
