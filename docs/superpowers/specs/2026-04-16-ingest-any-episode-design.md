# Ingest Any Episode — Design Spec

## Goal

Let the web UI user ingest any uningested episode, not just ones newer than the latest ingested. Show all gaps, provide a one-click "ingest latest" button, and a text input for ranges/lists.

## Current behavior

`_fetch_topics()` in `server.py` computes `not_ingested_episodes` but filters to only episodes newer than `max(ingested_eps)`. The ingest banner shows those and offers a single "Ingest now" button that sends the full list.

## Changes

### Server

**1. Remove the "newer than latest" filter** (`server.py:168-170`)

Delete the three lines that filter `not_ingested` to only episodes greater than `latest_ingested`. The `not_ingested` list already starts as `sorted(feed_eps - ingested_eps)` — that's the correct full set.

**2. Add `episode_input` to `IngestRequest`** (`server.py:40-43`)

Add an optional `episode_input: str = ""` field. Before calling `ingest_all()`, parse it into episode numbers and merge with `episode_numbers`.

Parsing rules:
- Commas separate entries: `"150, 210, 215"` → `[150, 210, 215]`
- Dashes denote inclusive ranges: `"150-200"` → `[150, 151, ..., 200]`
- Combined: `"150-200, 210, 215"` → `[150, ..., 200, 210, 215]`
- Whitespace is ignored
- Invalid tokens (non-numeric, backwards ranges like `"200-150"`, empty string) → 400 response with descriptive message

Parsing logic lives in a standalone function `parse_episode_input(s: str) -> list[int]` in `server.py` (small, endpoint-specific, not worth a separate module). Raises `ValueError` on bad input. The endpoint catches `ValueError` and returns 400.

Merged numbers are deduplicated and passed as `episode_numbers` to `ingest_all()`.

### Frontend (`index.html`)

**Banner layout** (three lines, all inside `#ingest-banner`):

```
Uningested: 142, 145, 150-198, 210-215

[Ingest latest]  [_150-200, 210______] [Ingest]

Ingesting Ep 210 (1/5): transcribing...
```

**Line 1 — Uningested summary**: A text span showing all uningested episode numbers, with consecutive numbers collapsed into ranges for readability (e.g., `150, 151, 152` → `150-152`). Built client-side from the `not_ingested_episodes` array returned by `/topics`.

**Line 2 — Controls**:
- "Ingest latest" button: sends `POST /ingest` with `episode_numbers: [max(not_ingested)]`. Ingests only the single most recent uningested episode.
- Text input: placeholder `"e.g. 150-200, 210, 215"`. 
- "Ingest" button beside it: sends `POST /ingest` with `episode_input` set to the text field value.
- Both buttons disable during ingestion (existing behavior extended to new button).

**Line 3 — Progress**: Existing progress display, unchanged. Shared by both ingestion triggers.

**Visibility**: Banner shows whenever `not_ingested_episodes.length > 0`. Hides when all episodes are ingested.

**Range collapse helper** (JS): A function that takes a sorted array of numbers and returns a display string. E.g., `[142, 145, 150, 151, 152, 153, 210, 211]` → `"142, 145, 150-153, 210-211"`. Used only for the summary line.

### What doesn't change

- `ingest_all()` and `_ingest_one()` in `ingest.py` — untouched
- Topic chips — untouched
- The `/ingest/status` polling and progress display — untouched
- Freshness polling — untouched
- CLI commands — untouched

## Testing

**Server — `parse_episode_input()` unit tests:**
- Single number: `"210"` → `[210]`
- Comma list: `"210, 215, 220"` → `[210, 215, 220]`
- Range: `"150-155"` → `[150, 151, 152, 153, 154, 155]`
- Mixed: `"150-155, 210, 220-222"` → `[150, 151, 152, 153, 154, 155, 210, 220, 221, 222]`
- Whitespace tolerance: `" 150 - 155 , 210 "` → same result
- Backwards range: `"200-150"` → `ValueError`
- Non-numeric: `"abc"` → `ValueError`
- Empty string: `""` → `[]`

**Server — endpoint integration test:**
- `POST /ingest` with `episode_input: "150-152"` starts ingestion with episodes 150, 151, 152

**Frontend — Playwright:**
- Banner shows all uningested episodes (including older gaps), not just newer-than-latest
- Uningested summary collapses ranges correctly
- "Ingest latest" button exists and is clickable
- Text input and "Ingest" button exist
- Text input sends `episode_input` field in request body

**Existing tests:**
- `test_web_episodes.py` ingest banner tests may need updates since the banner will now show all gaps instead of just newer episodes
