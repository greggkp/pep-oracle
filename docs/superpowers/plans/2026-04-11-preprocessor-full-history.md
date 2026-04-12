# Preprocessor Full History — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pass full conversation history to the preprocessor so it can resolve pronouns and references in follow-up questions.

**Architecture:** Change `preprocess_query` to accept `history` instead of `last_assistant_reply`, format the history into the prompt, and update the prompt to instruct Haiku to resolve pronouns. Update tests to match.

**Tech Stack:** Anthropic Claude Haiku, pytest

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `src/pep_oracle/query.py:29-57,85-160,163-219` | Change preprocessor signature, prompt, and `ask()` call site |
| Modify | `tests/test_query.py:270-293` | Update existing context test, add pronoun resolution test |

---

### Task 1: Update tests for full history in preprocessor

**Files:**
- Modify: `tests/test_query.py:270-293`

- [ ] **Step 1: Update `test_preprocess_query_receives_conversation_context`**

In `tests/test_query.py`, replace the test at lines 270-293:

```python
def test_preprocess_query_receives_conversation_context():
    """When last_assistant_reply is provided, it should be included in the prompt."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(
        text='{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "EU tariff response", "prefer_recent": false}'
    )]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("pep_oracle.query.get_ingestion_stats", return_value={
        "earliest_date": "2024-01-01", "latest_date": "2026-04-01",
        "earliest_episode": 200, "latest_episode": 253,
    }), patch("pep_oracle.query.get_client"), patch("pep_oracle.query.get_collection"):
        result = preprocess_query(
            "What about the EU response?",
            anthropic_client=mock_client,
            last_assistant_reply="In Episode 255, they discussed new tariff announcements...",
        )

    # Check that the prompt sent to Haiku includes the conversation context
    call_kwargs = mock_client.messages.create.call_args
    prompt_text = call_kwargs.kwargs["messages"][0]["content"]
    assert "In Episode 255, they discussed new tariff announcements" in prompt_text
    assert result["search_query"] == "EU tariff response"
```

With:

```python
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
```

- [ ] **Step 2: Add `test_preprocess_query_resolves_pronouns_from_history`**

Append after `test_preprocess_query_receives_conversation_context`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_query.py::test_preprocess_query_receives_conversation_context tests/test_query.py::test_preprocess_query_resolves_pronouns_from_history -v`
Expected: Both FAIL — `preprocess_query` doesn't accept `history` kwarg yet.

---

### Task 2: Update preprocessor to accept full history

**Files:**
- Modify: `src/pep_oracle/query.py:29-57,85-160,163-219`

- [ ] **Step 1: Update `PREPROCESS_PROMPT`**

In `src/pep_oracle/query.py`, replace lines 29-57 (the entire `PREPROCESS_PROMPT` string):

```python
PREPROCESS_PROMPT = """\
Extract search filters from this podcast question. Today's date is {today}.
The podcast has episodes from {earliest_date} to {latest_date} (episodes {earliest_ep} to {latest_ep}).

Return a JSON object with these fields:
- "episode_numbers": list of specific episode numbers mentioned (empty list if none)
- "after_date": earliest date to include as "YYYY-MM-DD" (null if no time constraint)
- "before_date": latest date to include as "YYYY-MM-DD" (null if no time constraint)
- "search_query": the core topic to search for (rewrite the question as a concise search phrase)
- "prefer_recent": true if the user wants the LATEST/most recent information, false otherwise

IMPORTANT: Set after_date for questions about current/recent/ongoing events. Words like \
"soon", "will", "currently", "right now", "these days", "latest", "recent", present tense \
questions about evolving situations — all imply the user wants RECENT episodes. \
Use after_date = 60 days before today for these. Only leave after_date as null for \
timeless/historical questions like "who is X?" or "when did they first discuss Y?".

Examples:
- "what did they say about Iran in episode 248?" → {{"episode_numbers": [248], "after_date": null, "before_date": null, "search_query": "Iran", "prefer_recent": false}}
- "will the war in Iran end soon?" → {{"episode_numbers": [], "after_date": "{recent_date}", "before_date": null, "search_query": "Iran war ending", "prefer_recent": true}}
- "what are they saying about tariffs?" → {{"episode_numbers": [], "after_date": "{recent_date}", "before_date": null, "search_query": "tariffs trade policy", "prefer_recent": true}}
- "latest on Iran?" → {{"episode_numbers": [], "after_date": "{recent_date}", "before_date": null, "search_query": "Iran latest developments", "prefer_recent": true}}
- "what were the main topics last month?" → {{"episode_numbers": [], "after_date": "{last_month_start}", "before_date": "{last_month_end}", "search_query": "main topics discussed", "prefer_recent": false}}
- "who is Dr Dave?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Dr Dave background who is", "prefer_recent": false}}
- "when did they first discuss the Iran situation?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Iran first discussion", "prefer_recent": false}}

Respond with ONLY the JSON object, no other text.

Question: {question}"""
```

With:

```python
PREPROCESS_PROMPT = """\
Extract search filters from this podcast question. Today's date is {today}.
The podcast has episodes from {earliest_date} to {latest_date} (episodes {earliest_ep} to {latest_ep}).

Return a JSON object with these fields:
- "episode_numbers": list of specific episode numbers mentioned (empty list if none)
- "after_date": earliest date to include as "YYYY-MM-DD" (null if no time constraint)
- "before_date": latest date to include as "YYYY-MM-DD" (null if no time constraint)
- "search_query": the core topic to search for (rewrite the question as a concise search phrase)
- "prefer_recent": true if the user wants the LATEST/most recent information, false otherwise

IMPORTANT: Set after_date for questions about current/recent/ongoing events. Words like \
"soon", "will", "currently", "right now", "these days", "latest", "recent", present tense \
questions about evolving situations — all imply the user wants RECENT episodes. \
Use after_date = 60 days before today for these. Only leave after_date as null for \
timeless/historical questions like "who is X?" or "when did they first discuss Y?".

If conversation history is provided, use it to resolve pronouns and references \
in the question. For example, if the user previously asked about "Pete Hegseth" \
and now asks "what does he think?", rewrite the search query to include "Pete Hegseth".

Examples:
- "what did they say about Iran in episode 248?" → {{"episode_numbers": [248], "after_date": null, "before_date": null, "search_query": "Iran", "prefer_recent": false}}
- "will the war in Iran end soon?" → {{"episode_numbers": [], "after_date": "{recent_date}", "before_date": null, "search_query": "Iran war ending", "prefer_recent": true}}
- "what are they saying about tariffs?" → {{"episode_numbers": [], "after_date": "{recent_date}", "before_date": null, "search_query": "tariffs trade policy", "prefer_recent": true}}
- "latest on Iran?" → {{"episode_numbers": [], "after_date": "{recent_date}", "before_date": null, "search_query": "Iran latest developments", "prefer_recent": true}}
- "what were the main topics last month?" → {{"episode_numbers": [], "after_date": "{last_month_start}", "before_date": "{last_month_end}", "search_query": "main topics discussed", "prefer_recent": false}}
- "who is Dr Dave?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Dr Dave background who is", "prefer_recent": false}}
- "when did they first discuss the Iran situation?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Iran first discussion", "prefer_recent": false}}
- Conversation: User asked about Pete Hegseth. Question: "what does he think about tariffs?" → {{"episode_numbers": [], "after_date": null, "before_date": null, "search_query": "Pete Hegseth tariffs opinion", "prefer_recent": false}}

Respond with ONLY the JSON object, no other text.

{history_block}Question: {question}"""
```

Note: The `{history_block}` placeholder replaces the old `Question: {question}` at the end. It will be empty string when no history, or `"Conversation so far:\nUser: ...\nAssistant: ...\n\n"` when history is present.

- [ ] **Step 2: Update `preprocess_query` signature and history formatting**

In `src/pep_oracle/query.py`, replace the `preprocess_query` function (lines 85-160):

```python
def preprocess_query(
    question: str,
    anthropic_client: anthropic.Anthropic | None = None,
    last_assistant_reply: str | None = None,
) -> dict:
    """Use a fast Claude model to extract time/episode filters from the question."""
    from datetime import date, timedelta

    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    today = date.today()
    # Get ingestion stats for context
    client = get_client()
    collection = get_collection(client)
    stats = get_ingestion_stats(collection)

    earliest_date = stats["earliest_date"] or "unknown"
    latest_date = stats["latest_date"] or "unknown"
    earliest_ep = stats["earliest_episode"] or "unknown"
    latest_ep = stats["latest_episode"] or "unknown"

    # Dates for the prompt examples
    recent_date = (today - timedelta(days=60)).isoformat()
    last_month_start = today.replace(day=1) - timedelta(days=1)
    last_month_start = last_month_start.replace(day=1).isoformat()
    last_month_end = (today.replace(day=1) - timedelta(days=1)).isoformat()

    prompt = PREPROCESS_PROMPT.format(
        today=today.isoformat(),
        earliest_date=earliest_date,
        latest_date=latest_date,
        earliest_ep=earliest_ep,
        latest_ep=latest_ep,
        recent_date=recent_date,
        last_month_start=last_month_start,
        last_month_end=last_month_end,
        question=question,
    )

    if last_assistant_reply:
        prompt = prompt.replace(
            f"Question: {question}",
            f"Previous assistant reply (for context): {last_assistant_reply}\n\nQuestion: {question}",
        )

    response = anthropic_client.messages.create(
        model=PREPROCESS_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]  # remove opening ```json line
            raw = raw.rsplit("```", 1)[0]  # remove closing ```
        parsed = json.loads(raw)
    except (json.JSONDecodeError, IndexError, ValueError):
        # Fall back to unfiltered search
        return {
            "episode_numbers": [],
            "after_date": None,
            "before_date": None,
            "search_query": question,
            "prefer_recent": False,
        }

    return {
        "episode_numbers": parsed.get("episode_numbers", []),
        "after_date": parsed.get("after_date"),
        "before_date": parsed.get("before_date"),
        "search_query": parsed.get("search_query", question),
        "prefer_recent": parsed.get("prefer_recent", False),
    }
```

With:

```python
def preprocess_query(
    question: str,
    anthropic_client: anthropic.Anthropic | None = None,
    history: list[dict] | None = None,
) -> dict:
    """Use a fast Claude model to extract time/episode filters from the question."""
    from datetime import date, timedelta

    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    today = date.today()
    # Get ingestion stats for context
    client = get_client()
    collection = get_collection(client)
    stats = get_ingestion_stats(collection)

    earliest_date = stats["earliest_date"] or "unknown"
    latest_date = stats["latest_date"] or "unknown"
    earliest_ep = stats["earliest_episode"] or "unknown"
    latest_ep = stats["latest_episode"] or "unknown"

    # Dates for the prompt examples
    recent_date = (today - timedelta(days=60)).isoformat()
    last_month_start = today.replace(day=1) - timedelta(days=1)
    last_month_start = last_month_start.replace(day=1).isoformat()
    last_month_end = (today.replace(day=1) - timedelta(days=1)).isoformat()

    # Format conversation history for the prompt
    history_block = ""
    if history:
        lines = []
        for msg in history:
            role = "User" if msg["role"] == "user" else "Assistant"
            lines.append(f"{role}: {msg['content']}")
        history_block = "Conversation so far:\n" + "\n".join(lines) + "\n\n"

    prompt = PREPROCESS_PROMPT.format(
        today=today.isoformat(),
        earliest_date=earliest_date,
        latest_date=latest_date,
        earliest_ep=earliest_ep,
        latest_ep=latest_ep,
        recent_date=recent_date,
        last_month_start=last_month_start,
        last_month_end=last_month_end,
        history_block=history_block,
        question=question,
    )

    response = anthropic_client.messages.create(
        model=PREPROCESS_MODEL,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]  # remove opening ```json line
            raw = raw.rsplit("```", 1)[0]  # remove closing ```
        parsed = json.loads(raw)
    except (json.JSONDecodeError, IndexError, ValueError):
        # Fall back to unfiltered search
        return {
            "episode_numbers": [],
            "after_date": None,
            "before_date": None,
            "search_query": question,
            "prefer_recent": False,
        }

    return {
        "episode_numbers": parsed.get("episode_numbers", []),
        "after_date": parsed.get("after_date"),
        "before_date": parsed.get("before_date"),
        "search_query": parsed.get("search_query", question),
        "prefer_recent": parsed.get("prefer_recent", False),
    }
```

- [ ] **Step 3: Update `ask()` to pass history instead of last_assistant_reply**

In `src/pep_oracle/query.py`, replace lines 163-187 (the beginning of `ask()` through the `preprocess_query` call):

```python
def ask(
    question: str,
    top_k: int = 10,
    model: str = QUERY_MODEL,
    anthropic_client: anthropic.Anthropic | None = None,
    openai_client=None,
    history: list[dict] | None = None,
) -> str:
    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    # Extract the last assistant reply from history for context
    last_assistant_reply = None
    if history:
        for msg in reversed(history):
            if msg["role"] == "assistant":
                last_assistant_reply = msg["content"]
                break

    # Pre-process to extract filters
    filters = preprocess_query(
        question,
        anthropic_client=anthropic_client,
        last_assistant_reply=last_assistant_reply,
    )
```

With:

```python
def ask(
    question: str,
    top_k: int = 10,
    model: str = QUERY_MODEL,
    anthropic_client: anthropic.Anthropic | None = None,
    openai_client=None,
    history: list[dict] | None = None,
) -> str:
    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    # Pre-process to extract filters
    filters = preprocess_query(
        question,
        anthropic_client=anthropic_client,
        history=history,
    )
```

- [ ] **Step 4: Run the two updated/new tests**

Run: `uv run pytest tests/test_query.py::test_preprocess_query_receives_conversation_context tests/test_query.py::test_preprocess_query_resolves_pronouns_from_history -v`
Expected: Both PASS.

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest -x -q`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/pep_oracle/query.py tests/test_query.py docs/superpowers/specs/2026-04-11-preprocessor-full-history-design.md docs/superpowers/plans/2026-04-11-preprocessor-full-history.md
git commit -m "fix: pass full conversation history to preprocessor for pronoun resolution"
```
