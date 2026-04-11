# Not-Ingested Episode Detection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show which episodes are not yet ingested in the web UI via amber-colored topic chips and an ingest banner, with feed-based detection so it works reliably regardless of Haiku's topic selection.

**Architecture:** The `/topics` endpoint gains feed-based ingestion detection — comparing all RSS episode numbers against ChromaDB metadata — and returns `not_ingested_episodes` alongside topics. The frontend code already exists as uncommitted changes and just needs the reliable server data. Tests at both endpoint and Playwright layers prevent future regressions.

**Tech Stack:** FastAPI, ChromaDB, Playwright, pytest, FastAPI TestClient

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `src/pep_oracle/server.py:114-135` | Fix `/topics` endpoint to use feed-based detection |
| Commit | `src/pep_oracle/web/index.html` | Commit existing unstaged frontend changes (CSS, banner HTML, JS) |
| Create | `tests/test_topics_endpoint.py` | FastAPI TestClient tests for `/topics` response shape |
| Modify | `tests/test_web_episodes.py` | Playwright tests for chip styling and ingest banner |

---

### Task 1: Server endpoint tests — response shape and basic detection

**Files:**
- Create: `tests/test_topics_endpoint.py`

These tests use FastAPI `TestClient` (synchronous, no server startup needed) with mocked `fetch_episodes`, `extract_topics`, and an in-memory ChromaDB collection.

- [ ] **Step 1: Write test file with fixtures and first test**

```python
"""Tests for the /topics endpoint's not_ingested_episodes detection."""

from datetime import datetime, timezone
from unittest.mock import patch

import chromadb
import pytest
from fastapi.testclient import TestClient

from pep_oracle.models import Chunk, Episode
from pep_oracle.store import add_chunks


def _make_episode(num, description="Episode description"):
    return Episode(
        guid=f"guid-{num}",
        title=f"Test Episode (Ep {num})",
        pub_date=datetime(2026, 1, num, tzinfo=timezone.utc),
        audio_url=f"https://example.com/ep{num}.mp3",
        description=description,
        duration_seconds=3600,
        episode_number=num,
    )


def _make_episode_no_number(guid, title, day):
    """Episode whose title doesn't match the episode number regex."""
    return Episode(
        guid=guid,
        title=title,
        pub_date=datetime(2026, 1, day, tzinfo=timezone.utc),
        audio_url="https://example.com/bonus.mp3",
        description="Bonus content",
        duration_seconds=1800,
        episode_number=None,
    )


FEED_EPISODES = [_make_episode(i) for i in range(1, 6)]

MOCK_TOPICS = [
    {"topic": "Topic A", "question": "What about A?", "episode_number": 5},
    {"topic": "Topic B", "question": "What about B?", "episode_number": 4},
]


def _ingest(collection, episode_number):
    chunk = Chunk(
        chunk_id=f"guid-{episode_number}_0000",
        episode_guid=f"guid-{episode_number}",
        text="Transcript text",
        episode_title=f"Test Episode (Ep {episode_number})",
        episode_date=f"2026-01-{episode_number:02d}",
        episode_number=episode_number,
        start_time=0.0,
        end_time=60.0,
    )
    add_chunks(collection, [chunk], [[0.1] * 10])


@pytest.fixture()
def client_and_collection():
    """FastAPI TestClient with mocked feed, topics, and in-memory ChromaDB."""
    chroma_client = chromadb.Client()
    collection = chroma_client.get_or_create_collection(
        name="pep_oracle", metadata={"hnsw:space": "cosine"}
    )

    with (
        patch("pep_oracle.server.fetch_episodes", return_value=FEED_EPISODES),
        patch("pep_oracle.server.extract_topics", return_value=MOCK_TOPICS),
        patch("pep_oracle.server._get_fresh_collection", return_value=collection),
    ):
        from pep_oracle.server import app

        yield TestClient(app), collection


def test_topics_response_contains_not_ingested_key(client_and_collection):
    """The /topics response must include the not_ingested_episodes field."""
    client, _ = client_and_collection
    resp = client.get("/topics")
    assert resp.status_code == 200
    data = resp.json()
    assert "not_ingested_episodes" in data
    assert "topics" in data


def test_topics_no_episodes_ingested(client_and_collection):
    """With no episodes ingested, all feed episode numbers appear in not_ingested_episodes."""
    client, _ = client_and_collection
    resp = client.get("/topics")
    data = resp.json()
    assert sorted(data["not_ingested_episodes"]) == [1, 2, 3, 4, 5]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_topics_endpoint.py -v`
Expected: FAIL — the current `/topics` endpoint uses topic-only detection, not feed-based, so `test_topics_no_episodes_ingested` will return only `[4, 5]` (topic episode numbers) instead of `[1, 2, 3, 4, 5]`.

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_topics_endpoint.py
git commit -m "test: add failing tests for feed-based not_ingested_episodes detection"
```

---

### Task 2: Fix the `/topics` endpoint to use feed-based detection

**Files:**
- Modify: `src/pep_oracle/server.py:114-135`

- [ ] **Step 1: Replace the topic-only detection with feed-based detection**

In `src/pep_oracle/server.py`, replace the current `api_topics` function (lines 114-135) with:

```python
@app.get("/topics")
async def api_topics():
    def _topics():
        episodes = fetch_episodes()
        topics = extract_topics(episodes)
        # Feed-based detection: compare ALL feed episodes against ChromaDB
        feed_eps = {ep.episode_number for ep in episodes if ep.episode_number is not None}
        try:
            collection = _get_fresh_collection()
            ingested_eps = set()
            all_meta = collection.get(include=["metadatas"])
            for meta in all_meta["metadatas"]:
                ep_num = meta.get("episode_number", 0)
                if ep_num:
                    ingested_eps.add(ep_num)
        except Exception:
            ingested_eps = set()
        not_ingested = sorted(feed_eps - ingested_eps)
        return topics, not_ingested

    topics, not_ingested = await asyncio.to_thread(_topics)
    return {"topics": topics, "not_ingested_episodes": not_ingested}
```

The key change: `feed_eps` is built from ALL episodes in the RSS feed (filtering out those with `episode_number is None`), not just the episodes Haiku chose for topics.

- [ ] **Step 2: Run the endpoint tests to verify they pass**

Run: `uv run pytest tests/test_topics_endpoint.py -v`
Expected: PASS — both tests should pass now.

- [ ] **Step 3: Commit**

```bash
git add src/pep_oracle/server.py
git commit -m "fix: use feed-based detection for not_ingested_episodes in /topics"
```

---

### Task 3: Add remaining endpoint tests

**Files:**
- Modify: `tests/test_topics_endpoint.py`

- [ ] **Step 1: Add tests for partial ingestion, full ingestion, ChromaDB failure, and type consistency**

Append to `tests/test_topics_endpoint.py`:

```python
def test_topics_some_episodes_ingested(client_and_collection):
    """With some episodes ingested, only un-ingested ones appear."""
    client, collection = client_and_collection
    _ingest(collection, 1)
    _ingest(collection, 3)
    _ingest(collection, 5)
    resp = client.get("/topics")
    data = resp.json()
    assert sorted(data["not_ingested_episodes"]) == [2, 4]


def test_topics_all_episodes_ingested(client_and_collection):
    """With all episodes ingested, not_ingested_episodes is empty."""
    client, collection = client_and_collection
    for i in range(1, 6):
        _ingest(collection, i)
    resp = client.get("/topics")
    data = resp.json()
    assert data["not_ingested_episodes"] == []


def test_topics_chromadb_failure_returns_all_as_not_ingested():
    """If ChromaDB fails, all feed episodes are treated as not-ingested."""
    def _broken_collection():
        raise RuntimeError("ChromaDB unavailable")

    with (
        patch("pep_oracle.server.fetch_episodes", return_value=FEED_EPISODES),
        patch("pep_oracle.server.extract_topics", return_value=MOCK_TOPICS),
        patch("pep_oracle.server._get_fresh_collection", side_effect=_broken_collection),
    ):
        from pep_oracle.server import app

        client = TestClient(app)
        resp = client.get("/topics")
        data = resp.json()
        assert sorted(data["not_ingested_episodes"]) == [1, 2, 3, 4, 5]


def test_topics_not_ingested_episodes_are_ints(client_and_collection):
    """Episode numbers in not_ingested_episodes must be ints, not strings."""
    client, _ = client_and_collection
    resp = client.get("/topics")
    data = resp.json()
    for ep_num in data["not_ingested_episodes"]:
        assert isinstance(ep_num, int)


def test_topics_episodes_without_number_excluded():
    """Episodes with episode_number=None are excluded from not_ingested_episodes."""
    episodes_with_bonus = FEED_EPISODES + [
        _make_episode_no_number("guid-bonus", "Bonus Episode", 10),
    ]
    with (
        patch("pep_oracle.server.fetch_episodes", return_value=episodes_with_bonus),
        patch("pep_oracle.server.extract_topics", return_value=MOCK_TOPICS),
        patch("pep_oracle.server._get_fresh_collection") as mock_col,
    ):
        mock_col.return_value.get.return_value = {"metadatas": []}
        from pep_oracle.server import app

        client = TestClient(app)
        resp = client.get("/topics")
        data = resp.json()
        # Should have episodes 1-5 but NOT the bonus (None episode_number)
        assert sorted(data["not_ingested_episodes"]) == [1, 2, 3, 4, 5]
```

- [ ] **Step 2: Run all endpoint tests**

Run: `uv run pytest tests/test_topics_endpoint.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_topics_endpoint.py
git commit -m "test: add comprehensive endpoint tests for not_ingested_episodes"
```

---

### Task 4: Commit the existing frontend changes

**Files:**
- Commit: `src/pep_oracle/web/index.html` (existing unstaged changes)

The HTML file already has the correct CSS (`.topic-chip.not-ingested` amber styling, `#ingest-banner` styles), banner HTML, and JavaScript for handling `not_ingested_episodes`. These were added as uncommitted work previously. This task just commits them.

- [ ] **Step 1: Verify the unstaged changes are correct**

Run: `git diff -- src/pep_oracle/web/index.html | head -20`
Expected: Shows the additions of `.topic-chip.not-ingested` CSS, `#ingest-banner` HTML, and related JavaScript.

- [ ] **Step 2: Commit the frontend changes**

```bash
git add src/pep_oracle/web/index.html
git commit -m "feat: add not-ingested chip styling and ingest banner to web UI"
```

---

### Task 5: Playwright tests for chip styling and ingest banner

**Files:**
- Modify: `tests/test_web_episodes.py`

These tests use the existing `server_with_collection` fixture which starts a real FastAPI server with mocked feed and in-memory ChromaDB. The fixture needs an additional mock for `extract_topics` since the `/topics` endpoint calls it.

- [ ] **Step 1: Add the extract_topics mock to the fixture**

In `tests/test_web_episodes.py`, the `server_with_collection` fixture's `patches` list (line 59) needs `extract_topics` mocked. Add this import at the top of the file:

```python
from pep_oracle.topics import extract_topics as _real_extract_topics
```

Then add to the `patches` list inside `server_with_collection`, after the existing patches:

```python
        patch("pep_oracle.server.extract_topics", return_value=[
            {"topic": "Topic from Ep 3", "question": "What about Ep 3?", "episode_number": 3},
            {"topic": "Topic from Ep 5", "question": "What about Ep 5?", "episode_number": 5},
        ]),
```

This gives the tests predictable topics — one for an episode we'll ingest (Ep 3) and one we won't (Ep 5).

- [ ] **Step 2: Write Playwright test for amber chip styling**

Append to `tests/test_web_episodes.py`:

```python
def test_not_ingested_chips_have_amber_styling(server_with_collection, browser):
    """Chips for un-ingested episodes should have the not-ingested CSS class."""
    base_url, collection = server_with_collection

    # Ingest episode 3 only — episode 5 remains un-ingested
    _ingest_into_collection(collection, "guid-3", 3)

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=10000)

    chips = page.query_selector_all(".topic-chip")
    assert len(chips) >= 2

    for chip in chips:
        ep_num = chip.get_attribute("data-episode")
        if ep_num == "5":
            assert "not-ingested" in chip.get_attribute("class")
        elif ep_num == "3":
            assert "not-ingested" not in chip.get_attribute("class")

    page.close()
```

- [ ] **Step 3: Write Playwright test for ingest banner visibility**

Append to `tests/test_web_episodes.py`:

```python
def test_ingest_banner_visible_when_not_ingested(server_with_collection, browser):
    """The ingest banner should appear when un-ingested episodes exist."""
    base_url, collection = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=10000)

    banner = page.query_selector("#ingest-banner")
    assert banner is not None
    # Banner should be visible (display: flex when episodes are not ingested)
    assert banner.is_visible()

    banner_text = banner.text_content()
    # All episodes 1-5 are not ingested, so banner should mention them
    assert "not yet ingested" in banner_text

    page.close()


def test_ingest_banner_hidden_when_all_ingested(server_with_collection, browser):
    """The ingest banner should not appear when all episodes are ingested."""
    base_url, collection = server_with_collection

    for i in range(1, 6):
        _ingest_into_collection(collection, f"guid-{i}", i)

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=10000)

    banner = page.query_selector("#ingest-banner")
    # Banner should be hidden (display: none)
    assert banner is None or not banner.is_visible()

    page.close()
```

- [ ] **Step 4: Write Playwright test for tooltip text**

Append to `tests/test_web_episodes.py`:

```python
def test_not_ingested_chip_tooltip(server_with_collection, browser):
    """Un-ingested chips should show '(not yet ingested)' in their tooltip."""
    base_url, collection = server_with_collection

    # Ingest episode 3 only
    _ingest_into_collection(collection, "guid-3", 3)

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=10000)

    chips = page.query_selector_all(".topic-chip")
    for chip in chips:
        ep_num = chip.get_attribute("data-episode")
        title = chip.get_attribute("title")
        if ep_num == "5":
            assert "(not yet ingested)" in title
        elif ep_num == "3":
            assert "(not yet ingested)" not in title

    page.close()
```

- [ ] **Step 5: Run all Playwright tests**

Run: `uv run pytest tests/test_web_episodes.py -v`
Expected: All tests PASS (existing + 4 new).

- [ ] **Step 6: Commit**

```bash
git add tests/test_web_episodes.py
git commit -m "test: add Playwright tests for not-ingested chip styling and ingest banner"
```

---

### Task 6: Run full test suite and final verification

**Files:** None (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -x -q`
Expected: All tests pass. This is what the pre-commit hook runs, so if it passes here, future commits are protected.

- [ ] **Step 2: Verify the server shows correct behavior**

Run: `curl -s http://localhost:8000/topics | python3 -m json.tool | head -30`
Expected: Response includes `"not_ingested_episodes"` with a list of episode numbers not yet in ChromaDB. The `"topics"` array should still be present.

- [ ] **Step 3: Verify in the browser**

Open `http://localhost:8000` in a browser. Check:
- Topic chips for un-ingested episodes appear in amber
- Topic chips for ingested episodes appear in blue
- Ingest banner is visible with correct episode numbers
- "Ingest now" button is present
