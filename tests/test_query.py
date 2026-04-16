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


def test_build_context_uses_speaker_text():
    results = [{
        "episode_title": "Test Episode",
        "episode_number": 251,
        "episode_date": "2026-03-21",
        "start_time": 10.0,
        "end_time": 60.0,
        "text": "I think tariffs are bad. I disagree.",
        "speaker_text": "[Chas] I think tariffs are bad. [Dave] I disagree.",
    }]
    ctx = build_context(results)
    assert "[Chas]" in ctx
    assert "[Dave]" in ctx
    assert "I think tariffs are bad" in ctx


def test_build_context_falls_back_to_text():
    results = [{
        "episode_title": "Test Episode",
        "episode_number": 251,
        "episode_date": "2026-03-21",
        "start_time": 10.0,
        "end_time": 60.0,
        "text": "Plain text without speakers.",
    }]
    ctx = build_context(results)
    assert "Plain text without speakers." in ctx


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


def test_preprocess_query_prefer_recent():
    """Pre-processor should return prefer_recent=True for recency-oriented questions."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"episode_numbers": [], "after_date": "2026-02-03", "before_date": null, "search_query": "Iran latest", "prefer_recent": true}'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query("latest on Iran?", anthropic_client=mock_client)

    assert result["prefer_recent"] is True


def test_preprocess_query_prefer_recent_default_false():
    """Pre-processor should default prefer_recent to False when not in response."""
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
        result = preprocess_query("what about Iran in ep 248?", anthropic_client=mock_client)

    assert result["prefer_recent"] is False


def test_ask_passes_history_to_claude():
    """ask() should build multi-turn messages from history + RAG-augmented current question."""
    mock_anthropic = MagicMock()
    mock_anthropic.messages.create.return_value = MagicMock(
        content=[MagicMock(text="Follow-up answer")]
    )

    history = [
        {"role": "user", "content": "What about tariffs?"},
        {"role": "assistant", "content": "In Episode 255, they discussed tariffs..."},
    ]

    with patch("pep_oracle.query.preprocess_query", return_value={
        "episode_numbers": [], "after_date": None, "before_date": None,
        "search_query": "EU response tariffs", "prefer_recent": False,
    }), patch("pep_oracle.query.embed_texts", return_value=[[0.1] * 10]), \
         patch("pep_oracle.query.get_client"), \
         patch("pep_oracle.query.get_collection"), \
         patch("pep_oracle.query.store_query", return_value=[{
            "episode_title": "Ep 255", "episode_number": 255,
            "episode_date": "2026-03-20", "start_time": 100.0,
            "end_time": 200.0, "text": "The EU responded to tariffs...",
         }]):
        from pep_oracle.query import ask
        result = ask(
            "What did Dr Dave think about the EU response?",
            anthropic_client=mock_anthropic,
            history=history,
        )

    assert result == "Follow-up answer"
    call_kwargs = mock_anthropic.messages.create.call_args
    messages = call_kwargs.kwargs["messages"]
    assert len(messages) == 3
    assert messages[0] == {"role": "user", "content": "What about tariffs?"}
    assert messages[1] == {"role": "assistant", "content": "In Episode 255, they discussed tariffs..."}
    assert messages[2]["role"] == "user"
    assert "TRANSCRIPT EXCERPTS" in messages[2]["content"]
    assert "What did Dr Dave think about the EU response?" in messages[2]["content"]


def test_preprocess_query_receives_conversation_context():
    """When history is provided, both user questions and assistant replies appear in the prompt."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "EU tariff response", "prefer_recent": false}'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    history = [
        {"role": "user", "content": "What about tariffs?"},
        {"role": "assistant", "content": "In Episode 255, they discussed new tariff announcements..."},
    ]

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query(
            "What about the EU response?",
            anthropic_client=mock_client,
            history=history,
        )

    call_kwargs = mock_client.messages.create.call_args
    prompt_text = call_kwargs.kwargs["messages"][0]["content"]
    assert "What about tariffs?" in prompt_text
    assert "In Episode 255, they discussed new tariff announcements" in prompt_text
    assert "Conversation so far:" in prompt_text
    assert result["search_query"] == "EU tariff response"


def test_preprocess_query_resolves_pronouns_from_history():
    """History containing entity names should appear in the prompt so Haiku can resolve pronouns."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Pete Hegseth tariffs opinion", "prefer_recent": false}'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    history = [
        {"role": "user", "content": "What did they say about Pete Hegseth?"},
        {"role": "assistant", "content": "In Episode 253, Chas and Dr Dave discussed Pete Hegseth's appointment..."},
    ]

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query(
            "what does he think about tariffs?",
            anthropic_client=mock_client,
            history=history,
        )

    call_kwargs = mock_client.messages.create.call_args
    prompt_text = call_kwargs.kwargs["messages"][0]["content"]
    # The history with "Pete Hegseth" must be in the prompt for Haiku to resolve "he"
    assert "Pete Hegseth" in prompt_text
    assert result["search_query"] == "Pete Hegseth tariffs opinion"


def test_ask_without_history_sends_single_message():
    """ask() without history should send a single user message (backward compat)."""
    mock_anthropic = MagicMock()
    mock_anthropic.messages.create.return_value = MagicMock(
        content=[MagicMock(text="Single answer")]
    )

    with patch("pep_oracle.query.preprocess_query", return_value={
        "episode_numbers": [], "after_date": None, "before_date": None,
        "search_query": "tariffs", "prefer_recent": False,
    }), patch("pep_oracle.query.embed_texts", return_value=[[0.1] * 10]), \
         patch("pep_oracle.query.get_client"), \
         patch("pep_oracle.query.get_collection"), \
         patch("pep_oracle.query.store_query", return_value=[{
            "episode_title": "Ep 255", "episode_number": 255,
            "episode_date": "2026-03-20", "start_time": 100.0,
            "end_time": 200.0, "text": "Tariff discussion...",
         }]):
        from pep_oracle.query import ask
        result = ask("What about tariffs?", anthropic_client=mock_anthropic)

    assert result == "Single answer"
    call_kwargs = mock_anthropic.messages.create.call_args
    messages = call_kwargs.kwargs["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"


def test_preprocess_query_detects_single_speaker():
    """Pre-processor should extract speaker name when question targets one person."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "tariffs", "prefer_recent": false, "speaker": "Chas", "compare_speakers": false}'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query("what did Chas say about tariffs?", anthropic_client=mock_client)

    assert result["speaker"] == "Chas"
    assert result["compare_speakers"] is False
    assert result["search_query"] == "tariffs"


def test_preprocess_query_detects_compare():
    """Pre-processor should detect compare intent."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "immigration", "prefer_recent": false, "speaker": null, "compare_speakers": true}'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query("Chas vs Dave on immigration", anthropic_client=mock_client)

    assert result["speaker"] is None
    assert result["compare_speakers"] is True


def test_preprocess_query_no_speaker_defaults():
    """Pre-processor should default speaker to None and compare to False."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "tariffs", "prefer_recent": false}'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query("what about tariffs?", anthropic_client=mock_client)

    assert result["speaker"] is None
    assert result["compare_speakers"] is False


def test_build_context_trims_to_single_speaker():
    """When speaker filter is active, build_context should trim to that speaker's portions."""
    results = [{
        "episode_title": "Test Episode",
        "episode_number": 251,
        "episode_date": "2026-03-21",
        "start_time": 10.0,
        "end_time": 250.0,
        "text": "I think tariffs are bad. I disagree strongly.",
        "speaker_text": "[Chas] I think tariffs are bad. [Dave] I disagree strongly.",
        "speakers": '[{"speaker": "Chas", "start": 10.0, "end": 130.0}, {"speaker": "Dave", "start": 130.0, "end": 250.0}]',
    }]
    ctx = build_context(results, speaker="Chas")
    assert "[Chas] I think tariffs are bad." in ctx
    assert "[Dave]" not in ctx


def test_build_context_trim_fallback_without_speakers():
    """Chunks without speakers data should include full text when speaker filter is active."""
    results = [{
        "episode_title": "Test Episode",
        "episode_number": 251,
        "episode_date": "2026-03-21",
        "start_time": 10.0,
        "end_time": 60.0,
        "text": "Full text without speaker info.",
    }]
    ctx = build_context(results, speaker="Chas")
    assert "Full text without speaker info." in ctx


def test_build_context_no_speaker_unchanged():
    """Without speaker filter, build_context behaves as before."""
    results = [{
        "episode_title": "Test Episode",
        "episode_number": 251,
        "episode_date": "2026-03-21",
        "start_time": 10.0,
        "end_time": 60.0,
        "text": "I think tariffs are bad. I disagree.",
        "speaker_text": "[Chas] I think tariffs are bad. [Dave] I disagree.",
    }]
    ctx = build_context(results)
    assert "[Chas]" in ctx
    assert "[Dave]" in ctx


def test_ask_single_speaker_passes_filter():
    """ask() should pass speaker filter to store_query and build_context."""
    mock_anthropic = MagicMock()
    mock_anthropic.messages.create.return_value = MagicMock(
        content=[MagicMock(text="Chas said tariffs are bad.")]
    )

    with patch("pep_oracle.query.preprocess_query", return_value={
        "episode_numbers": [], "after_date": None, "before_date": None,
        "search_query": "tariffs", "prefer_recent": False,
        "speaker": "Chas", "compare_speakers": False,
    }), patch("pep_oracle.query.embed_texts", return_value=[[0.1] * 10]), \
         patch("pep_oracle.query.get_client"), \
         patch("pep_oracle.query.get_collection"), \
         patch("pep_oracle.query.store_query", return_value=[{
            "episode_title": "Ep 255", "episode_number": 255,
            "episode_date": "2026-03-20", "start_time": 100.0,
            "end_time": 200.0, "text": "Tariff discussion...",
            "speaker_text": "[Chas] Tariffs are bad. [Dave] I disagree.",
            "speakers": '[{"speaker": "Chas", "start": 100.0, "end": 150.0}]',
         }]) as mock_store:
        from pep_oracle.query import ask
        ask("what did Chas say about tariffs?", anthropic_client=mock_anthropic)

    # Verify speaker was passed to store_query
    mock_store.assert_called_once()
    call_kwargs = mock_store.call_args
    assert call_kwargs.kwargs.get("speaker") == "Chas"
