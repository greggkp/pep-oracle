# Suggested Topics Feature

## Overview

Add a "suggested topics" feature to the web UI that presents clickable topic chips based on show notes from recent episodes. Clicking a topic populates the question input with a pre-written question the user can edit before submitting.

## Motivation

New users or returning visitors don't always know what to ask. Surfacing recent discussion topics lowers the barrier to engagement and highlights what the podcast has been covering lately.

## Architecture

### New module: `src/pep_oracle/topics.py`

A single public function:

```python
def extract_topics(
    episodes: list[Episode],
    count: int = 5,
    anthropic_client: anthropic.Anthropic | None = None,
) -> list[dict]:
```

- Filters to the `count` most recent episodes with non-empty `description` fields.
- Sends all descriptions in a single Claude Haiku call with a prompt that asks for 5-8 distinct discussion topics across the episodes.
- Each topic includes: a short label (3-6 words), a natural question a listener might ask, and the source episode number.
- Prompt instructs deduplication — no overlapping topics.
- Returns a list of dicts: `[{"topic": "...", "question": "...", "episode_number": N}]`.
- On any failure (API error, bad JSON), returns an empty list. The feature degrades silently.

### Backend: new `GET /topics` endpoint in `server.py`

- Calls `fetch_episodes()` to get the RSS feed.
- Passes the episode list to `extract_topics()`.
- Returns `{"topics": [...]}`.
- Runs the work in a thread via `asyncio.to_thread` (same pattern as other endpoints).

### Frontend: topic chips in `web/index.html`

- On page load, fetches `GET /topics`.
- Renders a row of clickable pill-shaped chips between the coverage line and the question form.
- Chips are styled as small rounded buttons, visually distinct from the main "Ask" button (lighter background, smaller text).
- On click: populates the question input with the topic's `question` value and focuses the input so the user can edit or submit.
- While loading, the chip area is hidden (no spinner or skeleton).
- If the fetch fails or returns an empty list, the chip area simply doesn't render.
- Chips wrap naturally on narrow screens.

## Data flow

```
Page load
  |
  v
GET /topics
  |
  v
server.py: fetch_episodes() -> last 5 episodes with descriptions
  |
  v
topics.py: extract_topics() -> single Haiku call -> parse JSON
  |
  v
Response: {"topics": [{"topic": "...", "question": "...", "episode_number": N}, ...]}
  |
  v
Frontend: render chips -> user clicks -> populate input -> user edits/submits -> existing /ask flow
```

## API contract

### `GET /topics`

**Response:**
```json
{
  "topics": [
    {
      "topic": "Trump tariff escalation",
      "question": "What are Chas and Dave saying about Trump's tariffs?",
      "episode_number": 251
    }
  ]
}
```

Returns `{"topics": []}` on failure or if no episodes have descriptions.

## Files changed

| File | Change |
|------|--------|
| `src/pep_oracle/topics.py` | New — `extract_topics()` with Haiku prompt |
| `src/pep_oracle/server.py` | New `GET /topics` endpoint |
| `src/pep_oracle/web/index.html` | Fetch `/topics`, render chips, populate input on click |
| `tests/test_topics.py` | New — unit tests for `extract_topics()` |
| `tests/test_server.py` | New or extended — integration test for `/topics` endpoint |

No changes to `feed.py`, `models.py`, `query.py`, `store.py`, or `cli.py`.

## Testing

- **`test_topics.py`**: Mock the Anthropic client. Test cases: valid JSON response, malformed JSON (returns empty list), empty descriptions filtered out, fewer than 5 episodes available.
- **`test_server.py`**: Mock `fetch_episodes` and `extract_topics`. Verify `/topics` returns correct shape, verify empty list on failure.
- No Playwright tests for the chips — frontend logic is trivial for MVP.

## Caching

Not included in MVP. If latency or API cost becomes a problem, add in-memory caching keyed on the latest episode GUID — invalidate when a new episode appears in the feed.

## Non-goals

- CLI command for topics (web UI only).
- Topic extraction from transcript chunks (show notes only).
- Persistent storage of topics.
- Topic categorization or grouping.
