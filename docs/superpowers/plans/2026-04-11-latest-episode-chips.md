# Latest Episode Chips — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change topic chip extraction to prioritize the latest episode, only using older episodes to fill up to the target count.

**Architecture:** Modify the `TOPIC_PROMPT` in `topics.py` to instruct Haiku to extract topics from the latest episode first, falling back to older episodes only if needed to reach the target. Update one test to verify the new prompt wording.

**Tech Stack:** Anthropic Claude Haiku, pytest

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `src/pep_oracle/topics.py:9-23` | Update `TOPIC_PROMPT` text |
| Modify | `tests/test_topics.py:22-42` | Update prompt assertion in existing test |

---

### Task 1: Update test to expect latest-episode-first prompt

**Files:**
- Modify: `tests/test_topics.py:22-42`

- [ ] **Step 1: Update `test_extract_topics_returns_parsed_topics` to verify the prompt prioritizes the latest episode**

In `tests/test_topics.py`, replace the test at lines 22-42:

```python
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
```

With:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_topics.py::test_extract_topics_returns_parsed_topics -v`
Expected: FAILS — the current prompt does not contain "LATEST" or the prioritization instruction.

---

### Task 2: Update the prompt and commit

**Files:**
- Modify: `src/pep_oracle/topics.py:9-23`

- [ ] **Step 1: Update `TOPIC_PROMPT`**

In `src/pep_oracle/topics.py`, replace lines 9-23:

```python
TOPIC_PROMPT = """\
Extract 5-8 distinct discussion topics from these podcast episode descriptions. \
Return a JSON array of objects, each with:
- "topic": a short label (3-6 words)
- "question": a natural question a podcast listener might ask about this topic \
(include a recency word like "latest", "recent", or "currently" since these are recent episodes)
- "episode_number": the episode number where this topic appears

Deduplicate: if multiple episodes discuss the same topic, pick the most recent one. \
No overlapping or redundant topics.

Episodes:
{episodes_text}

Respond with ONLY the JSON array, no other text."""
```

With:

```python
TOPIC_PROMPT = """\
Extract 5-8 distinct discussion topics from these podcast episode descriptions. \
The first episode listed is the LATEST. Extract as many topics as possible from \
the LATEST episode first. Only use older episodes to fill remaining slots if the \
latest episode yields fewer than 5 topics.

Return a JSON array of objects, each with:
- "topic": a short label (3-6 words)
- "question": a natural question a podcast listener might ask about this topic \
(include a recency word like "latest", "recent", or "currently" since these are recent episodes)
- "episode_number": the episode number where this topic appears

Deduplicate: if multiple episodes discuss the same topic, pick the most recent one. \
No overlapping or redundant topics.

Episodes:
{episodes_text}

Respond with ONLY the JSON array, no other text."""
```

- [ ] **Step 2: Run the updated test**

Run: `uv run pytest tests/test_topics.py::test_extract_topics_returns_parsed_topics -v`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest -x -q`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/pep_oracle/topics.py tests/test_topics.py docs/superpowers/specs/2026-04-11-latest-episode-chips-design.md docs/superpowers/plans/2026-04-11-latest-episode-chips.md
git commit -m "feat: prioritize latest episode for topic chip extraction"
```
