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
    assert result[0]["episode_number"] == 3
    mock_client.messages.create.assert_called_once()

    # Verify prompt instructs Haiku to prioritize the latest episode
    prompt_text = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "LATEST" in prompt_text
    assert "Extract as many topics as possible from the LATEST episode" in prompt_text


def test_extract_topics_malformed_json_returns_empty():
    """Haiku returns invalid JSON — extract_topics returns empty list."""
    episodes = [_make_episode(1, "Some description")]

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text="not valid json at all")
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)
    assert result == []


def test_extract_topics_api_error_returns_empty():
    """Anthropic API raises an exception — extract_topics returns empty list."""
    episodes = [_make_episode(1, "Some description")]

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("API down")

    result = extract_topics(episodes, anthropic_client=mock_client)
    assert result == []


def test_extract_topics_filters_empty_descriptions():
    """Episodes with empty or whitespace-only descriptions are skipped."""
    episodes = [
        _make_episode(3, "Real description here"),
        _make_episode(2, ""),
        _make_episode(1, "   "),
    ]
    haiku_response = '[{"topic": "Test topic", "question": "Test question?", "episode_number": 3}]'

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)
    assert len(result) == 1

    # Verify only the episode with a real description was sent to Haiku
    call_args = mock_client.messages.create.call_args
    prompt_text = call_args.kwargs["messages"][0]["content"]
    assert "Ep 3" in prompt_text
    assert "Ep 2" not in prompt_text
    assert "Ep 1" not in prompt_text


def test_extract_topics_no_episodes_returns_empty():
    """No episodes at all — returns empty list without calling Haiku."""
    mock_client = MagicMock()

    result = extract_topics([], anthropic_client=mock_client)
    assert result == []
    mock_client.messages.create.assert_not_called()


def test_extract_topics_all_empty_descriptions_returns_empty():
    """All episodes have empty descriptions — returns empty list without calling Haiku."""
    episodes = [_make_episode(1, ""), _make_episode(2, "")]
    mock_client = MagicMock()

    result = extract_topics(episodes, anthropic_client=mock_client)
    assert result == []
    mock_client.messages.create.assert_not_called()
