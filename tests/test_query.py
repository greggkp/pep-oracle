from unittest.mock import MagicMock, patch

from pep_oracle.query import build_context, format_timestamp, preprocess_query


def test_format_timestamp():
    assert format_timestamp(0.0) == "0:00:00"
    assert format_timestamp(3661.0) == "1:01:01"
    assert format_timestamp(None) == "?"


def test_format_timestamp_large():
    assert format_timestamp(12163.0) == "3:22:43"


def test_build_context_single_result():
    results = [{
        "episode_title": "MINING NEMO? PEP with Chas & Dr Dave (Ep 251, 20 March)",
        "episode_number": 251,
        "episode_date": "2026-03-21",
        "start_time": 10.0,
        "end_time": 235.0,
        "text": "Let's get peppy. Welcome to PEP 251.",
    }]
    ctx = build_context(results)
    assert "MINING NEMO?" in ctx
    assert "Ep 251" in ctx
    assert "2026-03-21" in ctx
    assert "0:00:10" in ctx
    assert "0:03:55" in ctx
    assert "Let's get peppy" in ctx


def test_build_context_no_episode_number():
    results = [{
        "episode_title": "Bonus Episode",
        "episode_number": None,
        "episode_date": "2026-01-01",
        "start_time": None,
        "end_time": None,
        "text": "Some content.",
    }]
    ctx = build_context(results)
    assert "Ep " not in ctx
    assert "?–?" in ctx


def test_build_context_multiple_results():
    results = [
        {
            "episode_title": "Ep 250",
            "episode_number": 250,
            "episode_date": "2026-03-14",
            "start_time": 100.0,
            "end_time": 300.0,
            "text": "First chunk.",
        },
        {
            "episode_title": "Ep 249",
            "episode_number": 249,
            "episode_date": "2026-03-11",
            "start_time": 500.0,
            "end_time": 700.0,
            "text": "Second chunk.",
        },
    ]
    ctx = build_context(results)
    assert "First chunk." in ctx
    assert "Second chunk." in ctx


def test_build_context_sorts_by_date_descending():
    """Results should be sorted newest-first regardless of input order."""
    results = [
        {
            "episode_title": "Old Ep",
            "episode_number": 220,
            "episode_date": "2025-06-01",
            "start_time": 0.0,
            "end_time": 60.0,
            "text": "Old content.",
        },
        {
            "episode_title": "New Ep",
            "episode_number": 248,
            "episode_date": "2026-03-06",
            "start_time": 0.0,
            "end_time": 60.0,
            "text": "New content.",
        },
    ]
    ctx = build_context(results)
    # New content should appear before old content
    assert ctx.index("New content.") < ctx.index("Old content.")


def test_preprocess_query_parses_episode_number():
    """Pre-processor should extract episode numbers from the question."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"episode_numbers": [248], "after_date": null, "before_date": null, "search_query": "Iran"}'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query("what did they say about Iran in episode 248?", anthropic_client=mock_client)

    assert result["episode_numbers"] == [248]
    assert result["search_query"] == "Iran"


def test_preprocess_query_parses_date_range():
    """Pre-processor should extract date ranges for time-sensitive questions."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"episode_numbers": [], "after_date": "2026-03-01", "before_date": null, "search_query": "Iran war"}'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query("will the war in Iran end soon?", anthropic_client=mock_client)

    assert result["after_date"] == "2026-03-01"
    assert result["search_query"] == "Iran war"


def test_preprocess_query_handles_bad_json():
    """If Claude returns invalid JSON, fall back to unfiltered search."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="I'm not sure what you mean")]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query("some question", anthropic_client=mock_client)

    assert result["episode_numbers"] == []
    assert result["after_date"] is None
    assert result["before_date"] is None
    assert result["search_query"] == "some question"


def test_preprocess_query_no_filters():
    """A general question should return no filters."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Dr Dave background"}'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query("who is Dr Dave?", anthropic_client=mock_client)

    assert result["episode_numbers"] == []
    assert result["after_date"] is None
    assert result["before_date"] is None


def test_preprocess_query_handles_markdown_code_fences():
    """Pre-processor should parse JSON wrapped in ```json code fences."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='```json\n{"episode_numbers": [], "after_date": "2026-02-03", "before_date": null, "search_query": "Iran war ending"}\n```'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query("will the war in Iran end soon?", anthropic_client=mock_client)

    assert result["after_date"] == "2026-02-03"
    assert result["search_query"] == "Iran war ending"
