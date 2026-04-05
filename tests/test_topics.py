"""Tests for topic extraction from episode show notes."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from pep_oracle.models import Episode
from pep_oracle.topics import extract_topics


def _make_episode(num, description=""):
    return Episode(
        guid=f"guid-{num}",
        title=f"Test Episode (Ep {num})",
        pub_date=datetime(2026, 3, num, tzinfo=timezone.utc),
        audio_url=f"https://example.com/ep{num}.mp3",
        description=description,
        duration_seconds=3600,
        episode_number=num,
    )


def test_extract_topics_returns_parsed_topics():
    """Haiku returns valid JSON — extract_topics parses and returns it."""
    episodes = [
        _make_episode(3, "Discussion about tariffs and trade war"),
        _make_episode(2, "Analysis of the latest Supreme Court rulings"),
        _make_episode(1, "Deep dive into immigration policy"),
    ]
    haiku_response = '[{"topic": "Tariffs and trade", "question": "What are Chas and Dave saying about tariffs?", "episode_number": 3}, {"topic": "Supreme Court rulings", "question": "What did they say about the Supreme Court?", "episode_number": 2}]'

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)

    assert len(result) == 2
    assert result[0]["topic"] == "Tariffs and trade"
    assert result[0]["question"] == "What are Chas and Dave saying about tariffs?"
    assert result[0]["episode_number"] == 3
    mock_client.messages.create.assert_called_once()
