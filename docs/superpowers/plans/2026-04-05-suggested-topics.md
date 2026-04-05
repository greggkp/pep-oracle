# Suggested Topics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add clickable topic chips to the web UI that surface recent podcast discussion topics from RSS show notes via Claude Haiku extraction.

**Architecture:** New `topics.py` module handles topic extraction from episode descriptions via a single Haiku call. A new `GET /topics` endpoint in `server.py` wires it up. The frontend fetches topics on page load and renders them as clickable chips that populate the question input.

**Tech Stack:** Python, anthropic SDK (Haiku), FastAPI, vanilla JS/HTML/CSS

**Spec:** `docs/superpowers/specs/2026-04-05-suggested-topics-design.md`

---

## File Structure

| File | Role |
|------|------|
| `src/pep_oracle/topics.py` | New — `extract_topics()` function, Haiku prompt, JSON parsing |
| `src/pep_oracle/server.py` | Modify — add `GET /topics` endpoint |
| `src/pep_oracle/web/index.html` | Modify — add topic chips UI |
| `tests/test_topics.py` | New — unit tests for `extract_topics()` |
| `tests/test_server.py` | Modify — add `/topics` endpoint test |

---

### Task 1: Topic extraction — happy path (TDD)

**Files:**
- Create: `tests/test_topics.py`
- Create: `src/pep_oracle/topics.py`

- [ ] **Step 1: Write the failing test for `extract_topics` happy path**

Create `tests/test_topics.py`:

```python
"""Tests for topic extraction from episode show notes."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_topics.py::test_extract_topics_returns_parsed_topics -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pep_oracle.topics'`

- [ ] **Step 3: Write minimal implementation**

Create `src/pep_oracle/topics.py`:

```python
import json

import anthropic

from pep_oracle.models import Episode

TOPIC_MODEL = "claude-haiku-4-5-20251001"

TOPIC_PROMPT = """\
Extract 5-8 distinct discussion topics from these podcast episode descriptions. \
Return a JSON array of objects, each with:
- "topic": a short label (3-6 words)
- "question": a natural question a podcast listener might ask about this topic
- "episode_number": the episode number where this topic appears

Deduplicate: if multiple episodes discuss the same topic, pick the most recent one. \
No overlapping or redundant topics.

Episodes:
{episodes_text}

Respond with ONLY the JSON array, no other text."""


def extract_topics(
    episodes: list[Episode],
    count: int = 5,
    anthropic_client: anthropic.Anthropic | None = None,
) -> list[dict]:
    """Extract discussion topics from recent episode descriptions via Haiku."""
    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    # Filter to episodes with descriptions, take most recent `count`
    with_desc = [ep for ep in episodes if ep.description and ep.description.strip()]
    recent = sorted(with_desc, key=lambda ep: ep.pub_date, reverse=True)[:count]

    if not recent:
        return []

    episodes_text = "\n".join(
        f"- Ep {ep.episode_number} ({ep.pub_date.strftime('%Y-%m-%d')}): {ep.description}"
        for ep in recent
    )

    response = anthropic_client.messages.create(
        model=TOPIC_MODEL,
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": TOPIC_PROMPT.format(episodes_text=episodes_text),
            }
        ],
    )

    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]

    return json.loads(raw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_topics.py::test_extract_topics_returns_parsed_topics -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/topics.py tests/test_topics.py
git commit -m "feat: add topic extraction from episode show notes (happy path)"
```

---

### Task 2: Topic extraction — error handling (TDD)

**Files:**
- Modify: `tests/test_topics.py`
- Modify: `src/pep_oracle/topics.py`

- [ ] **Step 1: Write failing tests for error cases**

Append to `tests/test_topics.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_topics.py -v`
Expected: `test_extract_topics_malformed_json_returns_empty` FAILS (json.loads raises, not caught), `test_extract_topics_api_error_returns_empty` FAILS (exception not caught). The filter/empty tests should already pass.

- [ ] **Step 3: Add error handling to `extract_topics`**

In `src/pep_oracle/topics.py`, wrap the API call and JSON parsing in a try/except. Replace the body of `extract_topics` from the `if not recent` check onward:

```python
def extract_topics(
    episodes: list[Episode],
    count: int = 5,
    anthropic_client: anthropic.Anthropic | None = None,
) -> list[dict]:
    """Extract discussion topics from recent episode descriptions via Haiku."""
    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic()

    # Filter to episodes with descriptions, take most recent `count`
    with_desc = [ep for ep in episodes if ep.description and ep.description.strip()]
    recent = sorted(with_desc, key=lambda ep: ep.pub_date, reverse=True)[:count]

    if not recent:
        return []

    episodes_text = "\n".join(
        f"- Ep {ep.episode_number} ({ep.pub_date.strftime('%Y-%m-%d')}): {ep.description}"
        for ep in recent
    )

    try:
        response = anthropic_client.messages.create(
            model=TOPIC_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": TOPIC_PROMPT.format(episodes_text=episodes_text),
                }
            ],
        )

        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0]

        return json.loads(raw)
    except Exception:
        return []
```

- [ ] **Step 4: Run all topic tests to verify they pass**

Run: `pytest tests/test_topics.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/topics.py tests/test_topics.py
git commit -m "feat: add error handling for topic extraction"
```

---

### Task 3: `/topics` server endpoint (TDD)

**Files:**
- Modify: `tests/test_server.py`
- Modify: `src/pep_oracle/server.py`

- [ ] **Step 1: Write the failing test for `/topics` endpoint**

Append to `tests/test_server.py`:

```python
def test_topics_returns_extracted_topics(client_and_collection):
    client, _ = client_and_collection
    mock_topics = [
        {"topic": "Tariffs", "question": "What about tariffs?", "episode_number": 3},
        {"topic": "Immigration", "question": "What about immigration?", "episode_number": 1},
    ]
    with patch("pep_oracle.server.extract_topics", return_value=mock_topics):
        resp = client.get("/topics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["topics"] == mock_topics


def test_topics_returns_empty_on_failure(client_and_collection):
    client, _ = client_and_collection
    with patch("pep_oracle.server.extract_topics", return_value=[]):
        resp = client.get("/topics")
    assert resp.status_code == 200
    assert resp.json()["topics"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server.py::test_topics_returns_extracted_topics tests/test_server.py::test_topics_returns_empty_on_failure -v`
Expected: FAIL — 404 (endpoint doesn't exist yet)

- [ ] **Step 3: Add the `/topics` endpoint to `server.py`**

Add this import at the top of `server.py`, after the existing imports:

```python
from pep_oracle.topics import extract_topics
```

Add this endpoint after the existing `api_episodes` function:

```python
@app.get("/topics")
async def api_topics():
    def _topics():
        episodes = fetch_episodes()
        return extract_topics(episodes)

    topics = await asyncio.to_thread(_topics)
    return {"topics": topics}
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `pytest tests/test_server.py::test_topics_returns_extracted_topics tests/test_server.py::test_topics_returns_empty_on_failure -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite to verify nothing broke**

Run: `pytest -x -q`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add src/pep_oracle/server.py tests/test_server.py
git commit -m "feat: add GET /topics endpoint"
```

---

### Task 4: Frontend topic chips

**Files:**
- Modify: `src/pep_oracle/web/index.html`

- [ ] **Step 1: Add the topic chips container to the HTML**

In `src/pep_oracle/web/index.html`, add this `div` between the `<p class="coverage">` line and the `<form>` tag:

```html
  <div id="topics" style="display:none; margin-bottom: 12px; display: flex; flex-wrap: wrap; gap: 8px;"></div>
```

Wait — the `display:none` and `display:flex` conflict. The container should start hidden and be shown by JS. Use this instead:

```html
  <div id="topics"></div>
```

- [ ] **Step 2: Add CSS for the topic chips**

Add these styles inside the `<style>` block, after the `.loading` rule:

```css
  #topics { display: none; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
  .topic-chip {
    padding: 6px 14px;
    font-size: 0.85rem;
    background: #e8f0fe;
    color: #1a56db;
    border: 1px solid #c3d9f7;
    border-radius: 20px;
    cursor: pointer;
    white-space: nowrap;
  }
  .topic-chip:hover { background: #d0e2fc; }
```

- [ ] **Step 3: Add the JavaScript to fetch and render topics**

Add this function in the `<script>` block, after the `loadStatus()` function definition and before the `loadStatus();` call:

```javascript
  async function loadTopics() {
    try {
      const resp = await fetch("/topics");
      if (!resp.ok) return;
      const data = await resp.json();
      if (!data.topics || data.topics.length === 0) return;

      const container = document.getElementById("topics");
      data.topics.forEach(t => {
        const chip = document.createElement("button");
        chip.className = "topic-chip";
        chip.textContent = t.topic;
        chip.title = "Ep " + t.episode_number;
        chip.addEventListener("click", () => {
          questionEl.value = t.question;
          questionEl.focus();
        });
        container.appendChild(chip);
      });
      container.style.display = "flex";
    } catch (e) {
      // Silent failure — topics are optional
    }
  }
```

Then update the bottom of the script to call both functions:

```javascript
  loadStatus();
  loadTopics();
```

- [ ] **Step 4: Verify manually (optional) or run existing tests**

Run: `pytest -x -q`
Expected: All tests pass (no frontend tests are affected)

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/web/index.html
git commit -m "feat: add suggested topic chips to web UI"
```

---

### Task 5: Integration verification and final commit

**Files:**
- All files from prior tasks

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`
Expected: All tests pass, including the new `test_topics.py` and `test_server.py` additions.

- [ ] **Step 2: Verify the file structure matches the spec**

Check that these files exist:
```bash
ls -la src/pep_oracle/topics.py tests/test_topics.py
```

Verify no unintended files were created:
```bash
git status
```

- [ ] **Step 3: Quick smoke test of the full flow (if server is available)**

If you can run the server locally:
```bash
pep-oracle-server &
curl -s http://localhost:8000/topics | python3 -m json.tool
```
Expected: JSON with a `topics` array (may be empty if no real API keys are configured).

- [ ] **Step 4: Final commit if any cleanup was needed**

```bash
git add -A
git commit -m "chore: suggested topics feature complete"
```
