# Speaker-Aware Retrieval Design

## Problem

The query pipeline cannot distinguish between speakers. Questions like "what did Chas say about tariffs?" or "Chas vs Dave on immigration" retrieve chunks based purely on topic similarity. Speaker identity is stored in chunk metadata (`speaker_text`, `speaker_turns`) but is only used when presenting context to Claude — retrieval itself is speaker-blind.

## Goals

- Retrieve chunks filtered by speaker when the question targets a specific speaker
- Support compare queries ("what did Chas say vs Dave") with dual retrieval
- Trim retrieved chunks to the target speaker's portions using existing `speaker_turns` data (hybrid approach)
- Preserve current behavior for non-speaker queries
- Keep chunk size at 4 minutes — good semantic signal, speaker isolation handled at query time

## Non-goals

- Improving diarization accuracy (pyannote alignment, speaker name mapping) — tracked as future enhancement in CLAUDE.md
- Speaker-aware chunk boundaries — mitigated by hybrid trim, tracked as future enhancement
- CLI diarize default change — keep explicit flag for manual use
- Web UI changes — queries go through the same pipeline
- Changing embedding model or chunk size

## Design

### 1. Metadata format change

**Current:** `speaker_list` stored as comma-separated string (e.g. `"Chas,Dave"`). ChromaDB cannot filter on substrings within string fields.

**New:** Replace with boolean fields per known speaker:

```python
# in _chunk_metadata()
if chunk.speaker_turns:
    unique = {t["speaker"] for t in chunk.speaker_turns}
    for speaker in unique:
        key = f"has_speaker_{speaker.lower().replace(' ', '_')}"
        meta[key] = True
```

This produces fields like `has_speaker_chas: True`, `has_speaker_dave: True`, `has_speaker_guest: True`.

The existing `speakers` field (JSON string of turn data) is kept — it's needed for the hybrid trim. The `speaker_list` comma string is removed.

**Migration:** Existing episodes require re-ingestion with `--force --diarize` to get the new metadata fields. Non-diarized episodes won't have these fields; queries against them fall back to speaker-blind behavior.

### 2. Preprocessor speaker detection

Add two fields to the preprocessor output:

- `speaker`: single speaker name when the question targets one person (`"Chas"`, `"Dave"`, or `null`)
- `compare_speakers`: `true` when the question asks for a comparison between speakers

Updated prompt examples:

```
- "what did Chas say about tariffs?" → {"speaker": "Chas", "compare_speakers": false, "search_query": "tariffs", ...}
- "does Dave think Trump will win?" → {"speaker": "Dave", "compare_speakers": false, "search_query": "Trump winning election", ...}
- "Chas vs Dave on immigration" → {"speaker": null, "compare_speakers": true, "search_query": "immigration", ...}
- "what was said about tariffs?" → {"speaker": null, "compare_speakers": false, "search_query": "tariffs", ...}
```

The preprocessor must strip speaker names from `search_query` so embeddings focus on topic, not speaker identity.

### 3. Query pipeline changes

**Single-speaker query** (e.g. `speaker: "Chas"`):

1. Preprocess extracts `speaker: "Chas"` and topic-only `search_query`
2. Embed the search query
3. Retrieve with `has_speaker_chas: True` added to `_build_where()` filters
4. In `build_context()`, trim each chunk to Chas's portions using `speaker_turns`
5. Send to Claude as normal

**Compare query** (`compare_speakers: true`):

1. Preprocess extracts `compare_speakers: true` and topic-only `search_query`
2. Embed the search query
3. Two retrievals: one with `has_speaker_chas: True`, one with `has_speaker_dave: True`, each using `top_k / 2`
4. Trim each set to the respective speaker's portions
5. Build context with two labeled sections: `CHAS'S STATEMENTS:` and `DAVE'S STATEMENTS:`
6. Claude prompt instructs comparison of perspectives

**No speaker intent** — unchanged current behavior.

### 4. Hybrid trim

Each chunk already stores `speakers` metadata — a JSON string of `[{"speaker": "Chas", "start": 120.5, "end": 145.2}, ...]`.

When a speaker filter is active, `build_context()` uses this to extract only the target speaker's text from the chunk. Implementation:

1. Parse the `speakers` JSON from chunk metadata to get turn boundaries
2. Use the `[Speaker]` labels already present in `speaker_text` to split text by speaker turns
3. Keep only the sections belonging to the target speaker
4. Present those portions, preserving the `[Speaker]` labels for Claude's attribution

Fallback: if a chunk has no `speaker_turns` data (non-diarized episode), include the full text.

### 5. `_build_where()` changes

Add optional `speaker` parameter:

```python
def _build_where(
    episode_number: int | None = None,
    episode_numbers: list[int] | None = None,
    speaker: str | None = None,
) -> dict | None:
```

When `speaker` is set, add `has_speaker_{name}: True` to the where clause. Combine with existing episode number filters using `$and` when both are present.

### 6. Server diarize default

Change `server.py` `IngestRequest.diarize` default from `False` to `True`. The web UI already sends `diarize: true`, the systemd timer passes `--diarize`, and the intent is that all ingestions are diarized. The API default should match.

### 7. Files changed

| File | Change |
|------|--------|
| `store.py` | `_chunk_metadata()`: boolean speaker fields, remove `speaker_list`. `_build_where()`: accept `speaker` param. `query()`: pass `speaker` through. |
| `query.py` | `preprocess_query()`: add speaker detection to prompt, parse `speaker`/`compare_speakers`. `ask()`: speaker-filtered retrieval, dual retrieval for compare. `build_context()`: hybrid trim using `speaker_turns`. |
| `server.py` | `IngestRequest.diarize` default to `True`. |
| Tests | Update `test_store.py`, `test_query.py`, `test_chunking.py` for new metadata format and speaker filtering. |

### 8. Migration

One-time re-ingestion of all episodes with `--force --diarize` to populate the new boolean speaker metadata fields. This replaces the old `speaker_list` format. Episodes ingested without diarization will lack speaker metadata and queries against them will use speaker-blind retrieval (current behavior).
