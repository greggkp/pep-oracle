# Scoped Ingest with Progress — Design Spec

## Problem

Two bugs:
1. The GUI's "Ingest now" button calls `ingest_all()` which ingests ALL un-ingested episodes, not just the newer ones shown in the UI. User clicks to ingest episode 255, server ingests 63 episodes.
2. Ingestion progress is invisible — the GUI shows "Ingesting..." with no detail, and the state is lost on page refresh.

## Solution

### 1. Scoped Ingestion

Add `episode_numbers: list[int] = []` to the `/ingest` request body. When provided, only those episodes are ingested. The GUI passes `notIngestedEpisodes` from the `/topics` response. CLI behavior (no episode filter) is unchanged.

**`server.py` changes:**
- Add `episode_numbers: list[int] = []` to `IngestRequest`
- Pass `episode_numbers=req.episode_numbers` to `ingest_all()`

**`ingest.py` changes:**
- Add `episode_numbers: list[int] | None = None` to `ingest_all()` signature
- When `episode_numbers` is non-empty, filter `to_process` to only episodes whose `episode_number` is in the list (applied after the existing GUID skip)

**`index.html` changes:**
- In the ingest button click handler, add `episode_numbers: notIngestedEpisodes` to the POST body

### 2. Progress Tracking

**`ingest.py` — progress callback:**
- Add `progress_callback: callable | None = None` parameter to `ingest_all()` and `_ingest_one()`
- `ingest_all()` calls `progress_callback` with episode-level info: starting episode N of M
- `_ingest_one()` calls `progress_callback` with step-level info: "downloading audio", "splitting audio", "transcribing part 3/9", "embedding N excerpts", "storing N excerpts"
- Callback signature: `callback(step: str)` — a human-readable string
- `ingest_all()` also calls callback with episode label before each episode, so the server knows which episode is current
- These replace or supplement the existing `click.echo()` calls — keep the echo calls for CLI, add callback calls alongside

**`server.py` — progress state:**
- Add module-level dict: `_ingest_progress = {"current_episode": "", "episodes_done": 0, "episodes_total": 0, "step": ""}`
- The `/ingest` async task provides a callback that updates `_ingest_progress`
- Reset `_ingest_progress` when ingestion starts and when it finishes

**`/ingest/status` response — new shape:**
```json
{
  "running": true,
  "current_episode": "Ep 255: TITLE...",
  "episodes_done": 0,
  "episodes_total": 1,
  "step": "transcribing part 4/9",
  "last_result": null
}
```

When not running, `current_episode`, `episodes_done`, `episodes_total`, and `step` are still present but empty/zero.

**`index.html` — progress display:**
- On page load, check `/ingest/status`. If `running` is true, show the ingest banner with progress
- Progress text format: "Ingesting Ep 255 (1/1): transcribing part 4/9"
- Poll every 3 seconds while running
- On completion, hide banner, refresh status, clear not-ingested styling as before

## CLI Unchanged

`ingest_all()` without `episode_numbers` or `progress_callback` behaves exactly as before. The CLI uses `click.echo()` for output, which is unaffected.

## Testing

- Test `/ingest` with `episode_numbers` only processes those episodes (mock `ingest_all`, verify it receives the episode list)
- Test `ingest_all` calls `progress_callback` at key points (mock the callback, verify it's called with expected strings)
- Test `/ingest/status` returns the new fields

## Scope

- Modify: `src/pep_oracle/ingest.py` (~20 lines — add params, add callback calls)
- Modify: `src/pep_oracle/server.py` (~15 lines — IngestRequest field, progress state, callback wiring)
- Modify: `src/pep_oracle/web/index.html` (~15 lines — send episode_numbers, poll on load, show progress)
- Add/modify: tests for ingest endpoint and progress
