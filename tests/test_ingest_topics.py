"""Tests for topic extraction during ingestion."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from pep_oracle.ingest import _ingest_one
from pep_oracle.models import Episode
from pep_oracle.topics import load_topics, save_topics


def _make_episode(num, description=""):
    return Episode(
        guid=f"guid-{num}",
        title=f"Test Episode (Ep {num})",
        pub_date=datetime(2026, 1, num, tzinfo=timezone.utc),
        audio_url=f"https://example.com/ep{num}.mp3",
        description=description,
        duration_seconds=3600,
        episode_number=num,
    )


@patch("pep_oracle.ingest.get_transcript")
@patch("pep_oracle.ingest.chunk_transcript")
@patch("pep_oracle.ingest.embed_texts")
@patch("pep_oracle.ingest.add_chunks")
def test_ingest_saves_topics(mock_add, mock_embed, mock_chunk, mock_transcript, tmp_path):
    """After successful ingestion, topics are saved to topics.json."""
    topics_path = tmp_path / "topics.json"
    ep = _make_episode(
        3,
        "<p>Timestamps:<br />"
        "0:00 - Introducing: Dr Dave<br />"
        "1:06:30 - Cuba<br />"
        "1:23:04 - Iran Latest</p>",
    )

    mock_transcript.return_value = (
        [{"start": 0, "end": 60, "text": "Hello"}],
        "whisper",
    )
    mock_chunk.return_value = [MagicMock(text="Hello")]
    mock_embed.return_value = [[0.1] * 10]
    collection = MagicMock()

    ok, topic_entry = _ingest_one(ep, collection)

    assert ok
    assert topic_entry is not None
    save_topics([topic_entry], topics_path)

    loaded = load_topics(topics_path)
    assert len(loaded) == 1
    assert loaded[0]["episode_number"] == 3
    assert "Cuba" in loaded[0]["topics"]
    assert "Iran Latest" in loaded[0]["topics"]


@patch("pep_oracle.ingest.get_transcript")
@patch("pep_oracle.ingest.chunk_transcript")
@patch("pep_oracle.ingest.embed_texts")
@patch("pep_oracle.ingest.add_chunks")
def test_ingest_no_topics_when_no_timestamps(mock_add, mock_embed, mock_chunk, mock_transcript, tmp_path):
    """Episodes without timestamps don't write to topics.json."""
    topics_path = tmp_path / "topics.json"
    ep = _make_episode(3, "No timestamps here")

    mock_transcript.return_value = (
        [{"start": 0, "end": 60, "text": "Hello"}],
        "whisper",
    )
    mock_chunk.return_value = [MagicMock(text="Hello")]
    mock_embed.return_value = [[0.1] * 10]
    collection = MagicMock()

    ok, topic_entry = _ingest_one(ep, collection)

    assert ok
    assert topic_entry is None
    assert not topics_path.exists()
