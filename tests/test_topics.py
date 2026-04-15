"""Tests for topic extraction from episode show notes."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from pep_oracle.models import Episode
from pep_oracle.topics import extract_topics, parse_description_topics


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


def test_extract_topics_sends_parsed_labels_to_haiku():
    """extract_topics sends parsed timestamp labels (not raw descriptions) to Haiku."""
    episodes = [
        _make_episode(
            3,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "3:57 - Gratefuls (Sliwa)<br />"
            "25:19 - Not Normal (Ballroom, Money)<br />"
            "1:06:30 - Cuba<br />"
            "1:23:04 - Iran Latest</p>",
        ),
        _make_episode(
            2,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Elle Hardy<br />"
            "8:57 - Kristi Noem Sacked<br />"
            "1:10:19 - Ukraine Corner</p>",
        ),
    ]
    haiku_response = '[{"topic": "Cuba", "question": "What did they discuss about Cuba recently?", "episode_number": 3}]'

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)

    assert len(result["topics"]) == 1
    assert result["topics"][0]["topic"] == "Cuba"
    mock_client.messages.create.assert_called_once()

    prompt_text = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    # Parsed labels should appear in the prompt
    assert "Cuba" in prompt_text
    assert "Iran Latest" in prompt_text
    assert "Kristi Noem Sacked" in prompt_text
    # Meta-segments should NOT appear
    assert "Introducing" not in prompt_text
    assert "Gratefuls" not in prompt_text
    # Raw HTML/description noise should NOT appear
    assert "<p>" not in prompt_text
    assert "<br />" not in prompt_text


def test_extract_topics_prompt_includes_segment_explanations():
    """The Haiku prompt explains Unleashed, Correspondence, Not Normal, Stats Nug, and Policy Time."""
    episodes = [
        _make_episode(
            3,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "1:06:30 - Cuba</p>",
        ),
    ]
    haiku_response = '[{"topic": "Cuba", "question": "Q?", "episode_number": 3}]'

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    extract_topics(episodes, anthropic_client=mock_client)

    prompt_text = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Unleashed" in prompt_text
    assert "Correspondence" in prompt_text
    assert "Not Normal" in prompt_text
    assert "Stats Nug" in prompt_text
    assert "Policy Time" in prompt_text
    assert "Do NOT paraphrase" in prompt_text


def test_extract_topics_skips_episodes_without_timestamps():
    """Episodes whose descriptions have no timestamp section are excluded from the Haiku prompt."""
    episodes = [
        _make_episode(
            3,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "1:06:30 - Cuba</p>",
        ),
        _make_episode(2, "Plain description with no timestamps at all"),
    ]
    haiku_response = '[{"topic": "Cuba", "question": "Q?", "episode_number": 3}]'

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    extract_topics(episodes, anthropic_client=mock_client)

    prompt_text = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Ep 3" in prompt_text
    assert "Ep 2" not in prompt_text


def test_extract_topics_filters_empty_descriptions():
    """Episodes with empty or whitespace-only descriptions are skipped."""
    episodes = [
        _make_episode(
            3,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "25:19 - Tariffs Discussion</p>",
        ),
        _make_episode(2, ""),
        _make_episode(1, "   "),
    ]
    haiku_response = '[{"topic": "Tariffs Discussion", "question": "What about tariffs?", "episode_number": 3}]'

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)
    assert len(result["topics"]) == 1

    # Verify only the episode with a real description was sent to Haiku
    call_args = mock_client.messages.create.call_args
    prompt_text = call_args.kwargs["messages"][0]["content"]
    assert "Ep 3" in prompt_text
    assert "Ep 2" not in prompt_text
    assert "Ep 1" not in prompt_text


def test_extract_topics_malformed_json_returns_empty():
    """Haiku returns invalid JSON — extract_topics returns empty dict."""
    episodes = [
        _make_episode(
            1,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Host<br />"
            "10:00 - Some Topic</p>",
        ),
    ]

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text="not valid json at all")
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)
    assert result == {"topics": [], "pool": []}


def test_extract_topics_api_error_returns_empty():
    """Anthropic API raises an exception — extract_topics returns empty dict."""
    episodes = [
        _make_episode(
            1,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Host<br />"
            "10:00 - Some Topic</p>",
        ),
    ]

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = Exception("API down")

    result = extract_topics(episodes, anthropic_client=mock_client)
    assert result == {"topics": [], "pool": []}


def test_extract_topics_no_episodes_returns_empty():
    """No episodes at all — returns empty dict without calling Haiku."""
    mock_client = MagicMock()

    result = extract_topics([], anthropic_client=mock_client)
    assert result == {"topics": [], "pool": []}
    mock_client.messages.create.assert_not_called()


def test_extract_topics_all_empty_descriptions_returns_empty():
    """All episodes have empty descriptions — returns empty dict without calling Haiku."""
    episodes = [_make_episode(1, ""), _make_episode(2, "")]
    mock_client = MagicMock()

    result = extract_topics(episodes, anthropic_client=mock_client)
    assert result == {"topics": [], "pool": []}
    mock_client.messages.create.assert_not_called()


def test_extract_topics_all_descriptions_lack_timestamps():
    """If all episodes have descriptions but none have timestamps, return empty dict without calling Haiku."""
    episodes = [
        _make_episode(2, "Just a plain description"),
        _make_episode(1, "Another plain description"),
    ]
    mock_client = MagicMock()

    result = extract_topics(episodes, anthropic_client=mock_client)
    assert result == {"topics": [], "pool": []}
    mock_client.messages.create.assert_not_called()


def test_extract_topics_returns_dict_with_topics_and_pool():
    """extract_topics returns a dict with 'topics' (Haiku-selected) and 'pool' (remaining labels)."""
    episodes = [
        _make_episode(
            3,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "3:57 - Gratefuls (Sliwa)<br />"
            "25:19 - Not Normal (Ballroom, Money)<br />"
            "1:06:30 - Cuba<br />"
            "1:23:04 - Iran Latest</p>",
        ),
    ]
    # Haiku only selects Cuba
    haiku_response = '[{"topic": "Cuba", "question": "What did they discuss about Cuba recently?", "episode_number": 3}]'

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)

    assert isinstance(result, dict)
    assert "topics" in result
    assert "pool" in result
    # Haiku-selected topic is in topics
    assert len(result["topics"]) == 1
    assert result["topics"][0]["topic"] == "Cuba"
    # Remaining parsed labels (not selected by Haiku) are in pool
    pool_topics = [entry["topic"] for entry in result["pool"]]
    assert "Iran Latest" in pool_topics
    # Haiku-selected label is NOT in pool
    assert "Cuba" not in pool_topics
    # Roundup segments are filtered from pool
    assert "Not Normal (Ballroom, Money)" not in pool_topics


def test_extract_topics_pool_entries_have_correct_shape():
    """Pool entries have topic, question, and episode_number fields; question mentions latest episode."""
    episodes = [
        _make_episode(
            3,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "25:19 - Not Normal (Ballroom, Money)<br />"
            "1:06:30 - Cuba<br />"
            "1:23:04 - Iran Latest</p>",
        ),
    ]
    # Haiku only picks Cuba — the other two go to pool
    haiku_response = '[{"topic": "Cuba", "question": "What about Cuba recently?", "episode_number": 3}]'

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)

    for entry in result["pool"]:
        assert "topic" in entry
        assert "question" in entry
        assert "episode_number" in entry
        assert "latest episode" in entry["question"]


def test_extract_topics_pool_filters_roundup_segments():
    """Correspondence, Not Normal, and bare Unleashed labels are excluded from pool."""
    episodes = [
        _make_episode(
            3,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "16:28 - Correspondence (Corrections, Stings)<br />"
            "25:19 - Not Normal (Ballroom, Money)<br />"
            "45:00 - Unleashed with Lachie<br />"
            "1:06:30 - Unleashed: Birthright Citizenship Cont.<br />"
            "1:23:04 - Iran Latest</p>",
        ),
    ]
    haiku_response = '[{"topic": "Iran Latest", "question": "Q?", "episode_number": 3}]'

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)
    pool_topics = [e["topic"] for e in result["pool"]]

    # Segment names filtered out, but subtopics extracted
    assert not any(t.startswith("Correspondence") for t in pool_topics)
    assert not any(t.startswith("Not Normal") for t in pool_topics)
    # Subtopics from segments are preserved
    assert "Corrections" in pool_topics
    assert "Stings" in pool_topics
    # Bare "Unleashed with X" filtered out
    assert "Unleashed with Lachie" not in pool_topics
    # "Unleashed: Topic Cont." cleaned to just "Topic" (Cont. stripped)
    assert "Birthright Citizenship" in pool_topics
    assert "Birthright Citizenship Cont." not in pool_topics


def test_extract_topics_filters_roundup_from_curated_preserves_subtopics():
    """Segment names are stripped from curated topics but subtopics are preserved in pool."""
    episodes = [
        _make_episode(
            3,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "25:19 - Not Normal (Flynn Settlement)<br />"
            "1:06:30 - Cuba</p>",
        ),
    ]
    haiku_response = (
        '['
        '{"topic": "Not Normal (Flynn Settlement)", "question": "Q?", "episode_number": 3},'
        '{"topic": "Cuba", "question": "What about Cuba?", "episode_number": 3}'
        ']'
    )

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)
    curated_topics = [t["topic"] for t in result["topics"]]
    pool_topics = [e["topic"] for e in result["pool"]]

    # Segment name stripped from curated
    assert "Not Normal (Flynn Settlement)" not in curated_topics
    assert "Cuba" in curated_topics
    # Subtopic preserved in pool
    assert "Flynn Settlement" in pool_topics


def test_extract_topics_filters_all_segment_names():
    """Stats Nug, Policy Time, Correspondence, and Not Normal are all filtered as segments."""
    episodes = [
        _make_episode(
            3,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "10:00 - Stats Nug (Emergency Response)<br />"
            "20:00 - Policy Time (Healthcare Reform)<br />"
            "30:00 - Correspondence (Corrections, Stings)<br />"
            "40:00 - Not Normal (Airport, Sharpies)<br />"
            "1:06:30 - Cuba</p>",
        ),
    ]
    haiku_response = (
        '['
        '{"topic": "Stats Nug (Emergency Response)", "question": "Q?", "episode_number": 3},'
        '{"topic": "Cuba", "question": "What about Cuba?", "episode_number": 3}'
        ']'
    )

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)
    curated_topics = [t["topic"] for t in result["topics"]]
    pool_topics = [e["topic"] for e in result["pool"]]
    all_labels = curated_topics + pool_topics

    # No segment names in any chips
    for label in all_labels:
        assert not label.startswith("Stats Nug"), f"Segment name leaked: {label}"
        assert not label.startswith("Policy Time"), f"Segment name leaked: {label}"
        assert not label.startswith("Correspondence"), f"Segment name leaked: {label}"
        assert not label.startswith("Not Normal"), f"Segment name leaked: {label}"

    # But subtopics are preserved in pool
    assert "Emergency Response" in pool_topics
    assert "Healthcare Reform" in pool_topics


def test_extract_topics_pool_empty_when_all_selected():
    """When Haiku selects all parsed labels, pool is empty."""
    episodes = [
        _make_episode(
            1,
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Host<br />"
            "10:00 - Only Topic</p>",
        ),
    ]
    # Haiku selects the only non-meta label
    haiku_response = '[{"topic": "Only Topic", "question": "What about Only Topic recently?", "episode_number": 1}]'

    mock_client = MagicMock()
    mock_client.messages.create.return_value.content = [
        MagicMock(text=haiku_response)
    ]

    result = extract_topics(episodes, anthropic_client=mock_client)

    assert result["pool"] == []


def test_parse_description_topics_extracts_labels():
    """Extracts topic labels from HTML description with timestamps."""
    description = (
        "<p>Chas &amp; Dr Dave discuss things.</p> "
        "<p>Timestamps:<br />"
        "0:00 - Introducing: Dr Dave<br />"
        "3:57 - Gratefuls (Sliwa, Colbert)<br />"
        "16:28 - Correspondence (Corrections, Stings, Noem v Miller)<br />"
        "25:19 - Not Normal (Ballroom, Money)<br />"
        "1:06:30 - Cuba<br />"
        "1:23:04 - Iran Latest</p> "
        "<p>Homework:</p>"
    )
    result = parse_description_topics(description)
    assert result == [
        "Correspondence (Corrections, Stings, Noem v Miller)",
        "Not Normal (Ballroom, Money)",
        "Cuba",
        "Iran Latest",
    ]


def test_parse_description_topics_cleans_trailing_noise():
    """Trailing 'Homework:' or 'SHOW LINKS:' appended to the last label is stripped."""
    description = (
        "<p>Timestamps:<br />"
        "0:00 - Introducing: Dr Dave<br />"
        "27:09 - Polling Update<br />"
        "3:15:17 - PBS/NPR Court Victory Homework:</p>"
    )
    result = parse_description_topics(description)
    assert result == ["Polling Update", "PBS/NPR Court Victory"]


def test_parse_description_topics_no_timestamps_section():
    """Description without 'Timestamps:' marker returns empty list."""
    description = "<p>Just a plain episode description with no timestamps.</p>"
    result = parse_description_topics(description)
    assert result == []


def test_parse_description_topics_empty_description():
    """Empty or whitespace description returns empty list."""
    assert parse_description_topics("") == []
    assert parse_description_topics("   ") == []


def test_parse_description_topics_filters_grateful_variants():
    """Both 'Grateful' and 'Gratefuls' are filtered out."""
    description = (
        "<p>Timestamps:<br />"
        "0:00 - Introducing: Elle Hardy<br />"
        "1:43 - Grateful (Andrew Lownie, Footy Players)<br />"
        "8:57 - Kristi Noem Sacked<br />"
        "1:10:19 - Ukraine Corner</p>"
    )
    result = parse_description_topics(description)
    assert result == ["Kristi Noem Sacked", "Ukraine Corner"]
