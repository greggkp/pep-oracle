# Not-Ingested Episode Detection — Design Spec

## Problem

The web UI shows topic chips from recent episodes but does not indicate which episodes are not yet ingested. This feature was partially implemented twice but lost to UI rewrites because no tests existed to catch the regression.

The uncommitted attempt coupled detection to Haiku's topic output — only episodes that Haiku happened to generate topics for could be flagged. This is unreliable because Haiku deduplicates topics across episodes.

## Solution

Feed-based detection: compare ALL RSS feed episodes against ChromaDB to determine which are not ingested, independent of topic extraction.

## Server Changes

### `/topics` endpoint

The endpoint already calls `fetch_episodes()` and `extract_topics()`. Add feed-based ingestion detection:

1. Get all episode numbers from the RSS feed (via `fetch_episodes()`)
2. Get all ingested episode numbers from ChromaDB metadata
3. Compute `not_ingested_episodes` as the set difference: feed episode numbers minus ingested episode numbers
4. Return alongside topics:

```json
{
  "topics": [...],
  "not_ingested_episodes": [258, 259, 260]
}
```

Use `_get_fresh_collection()` (already called by the endpoint) to ensure current ChromaDB state. Filter out episodes where `episode_number` is `None` (titles that don't match the episode number regex).

If ChromaDB access fails, treat all feed episodes as not-ingested (fail-open for the banner — better to show it unnecessarily than to hide it when episodes need ingestion).

## Frontend Changes

All frontend code already exists in the uncommitted `index.html` changes. No new frontend work needed — the existing JS correctly:

- Reads `data.not_ingested_episodes` from the `/topics` response
- Applies `.not-ingested` CSS class to chips whose `episode_number` is in the list
- Shows the ingest banner with episode numbers when the list is non-empty
- Handles the "Ingest now" button click, polling, and cleanup after ingestion completes

The CSS for `.topic-chip.not-ingested` (amber styling) and `#ingest-banner` also already exists in the uncommitted changes.

The only requirement is that the server returns the correct `not_ingested_episodes` list.

## Test Plan

### Layer 1 — Server endpoint tests (`test_server.py` or `test_topics_endpoint.py`)

Using FastAPI `TestClient` with mocked `fetch_episodes` and in-memory ChromaDB. No Playwright needed.

1. **Response shape**: `/topics` response contains `not_ingested_episodes` key
2. **No episodes ingested**: all feed episode numbers appear in `not_ingested_episodes`
3. **Some episodes ingested**: only un-ingested episode numbers appear
4. **All episodes ingested**: `not_ingested_episodes` is empty list
5. **ChromaDB failure**: gracefully returns all feed episodes as not-ingested
6. **Type consistency**: episode numbers in `not_ingested_episodes` are ints

### Layer 2 — Playwright integration tests (`test_web_episodes.py`)

Using the existing `server_with_collection` fixture pattern.

7. **Amber chips**: chips for un-ingested episodes have `.not-ingested` class
8. **Blue chips**: chips for ingested episodes do not have `.not-ingested` class
9. **Banner visible**: ingest banner shows when un-ingested episodes exist, with correct episode numbers
10. **Banner hidden**: ingest banner not visible when all episodes are ingested
11. **Tooltip text**: un-ingested chip tooltip includes "(not yet ingested)"

### Regression Prevention

These tests run in the default `pytest` suite. The existing pre-commit hook (`pytest -x -q`) blocks commits that break them. No additional tooling needed.

Tests assert observable behavior (CSS classes, banner visibility, response shape) rather than implementation details, so they remain valid across UI rewrites.

## Scope

- Server: modify `/topics` endpoint in `server.py` (~15 lines changed)
- Frontend: commit existing uncommitted HTML/CSS/JS changes (no new code)
- Tests: new test file for endpoint tests, additions to `test_web_episodes.py` for Playwright tests
- No changes to `topics.py`, `store.py`, `feed.py`, or any other module
