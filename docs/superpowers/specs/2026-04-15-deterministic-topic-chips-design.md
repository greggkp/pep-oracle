# Deterministic Topic Chips Design

## Problem

Topic chips are generated at runtime via a Haiku API call, which is expensive, non-deterministic (different chips on each cache refresh), and produces poor pool questions ("What did they discuss about X on the latest episode?" when X isn't from the latest episode). The chips should be extracted deterministically from show notes at ingestion time, persisted to disk, and served instantly.

## Solution

Replace the Haiku-based `extract_topics()` with a deterministic pipeline that runs during episode ingestion:

1. Parse timestamp labels from each episode's description (existing `parse_description_topics()`)
2. Clean labels: strip segment prefixes (Correspondence, Not Normal, Stats Nug, Policy Time), extract parenthetical subtopics, clean Unleashed entries, strip Cont. suffixes, filter meta-segments (Introducing, Gratefuls)
3. Store cleaned labels per episode in `~/.pep-oracle/topics.json`
4. `/topics` endpoint reads from file — no API call, instant response

## Data Shape

`~/.pep-oracle/topics.json`:

```json
{
  "episodes": [
    {
      "episode_number": 255,
      "date": "2026-04-10",
      "topics": [
        "Supreme Court Birthright Citizenship Case",
        "Pam Bondi's Firing",
        "The 10 Point Peace Plan",
        "Deadline Day in Iran"
      ]
    },
    {
      "episode_number": 254,
      "date": "2026-04-07",
      "topics": [
        "Hegseth Issues",
        "Iran Latest"
      ]
    }
  ]
}
```

- Sorted newest-first by episode number
- Topics within each episode in timestamp order (order they appear in show notes)
- Labels cleaned but no cross-episode deduplication — each episode's topics are independent
- Questions are not stored; generated at display time from label + episode number

## Topic Cleaning Pipeline

`clean_episode_topics(labels: list[str]) -> list[str]` processes a single episode's parsed labels:

1. Filter meta-segments: skip labels starting with "Introducing" or "Grateful"
2. Strip segment prefixes: for labels starting with Correspondence, Not Normal, Stats Nug, or Policy Time — extract parenthetical subtopics as individual labels, discard the segment name
3. Clean Unleashed: "Unleashed: Topic" becomes "Topic"; bare "Unleashed with X" is discarded
4. Strip "Cont." suffix from continuations

This is the same logic currently spread across `_SKIP_LABELS`, the pool-building loop, and the curated post-filter — consolidated into one function operating on a single episode. `parse_description_topics()` stays unchanged and continues to handle the HTML→labels extraction and meta-segment filtering (Introducing, Gratefuls).

Note: `parse_description_topics()` already filters Introducing and Gratefuls via `_SKIP_LABELS`. `clean_episode_topics()` handles the remaining cleaning (segment prefixes, Unleashed, Cont.) that was previously done in `extract_topics()`.

## Ingestion Integration

After each episode is successfully ingested in `_ingest_one()`:

1. Call `parse_description_topics(episode.description)` to get raw labels
2. Call `clean_episode_topics(labels)` to get cleaned labels
3. If labels exist, append to the topics file

After `ingest_all()` completes (not per-episode):

1. Read existing `topics.json` (may have topics from prior ingestions)
2. Merge in newly ingested episode topics
3. Sort episodes newest-first
4. Write back to `topics.json`

This means topics accumulate across ingestion runs. Re-ingesting with `--force` overwrites that episode's topics.

## Server Changes

### `_fetch_topics()` replacement

The function currently calls `extract_topics()` (Haiku). Replace with:

1. Read `topics.json` from disk
2. Compute `not_ingested_episodes` (still live: feed episodes vs ChromaDB)
3. Return `{"episodes": [...], "not_ingested_episodes": [...]}`

No cache TTL needed for the topic data itself — it's static until next ingestion. `not_ingested_episodes` still uses the existing cache mechanism.

### `/topics` response shape change

Old:
```json
{
  "topics": [{"topic": "...", "question": "...", "episode_number": N}, ...],
  "pool": [...],
  "not_ingested_episodes": [...],
  "stale": true/false
}
```

New:
```json
{
  "episodes": [
    {"episode_number": 255, "date": "2026-04-10", "topics": ["Cuba", "Iran Latest", ...]},
    {"episode_number": 254, "date": "2026-04-07", "topics": ["Hegseth Issues", ...]}
  ],
  "not_ingested_episodes": [...],
  "stale": true/false
}
```

## Frontend Changes

### Chip display

- Initial view: show the latest episode's topics as chips
- Each chip shows topic and episode number inline: `"Cuba · Ep 253"`
- "More..." button at the end (dashed border style, existing CSS)
- Clicking "More..." adds the next episode's topics, keeps "More..." if more episodes remain

### Chip click behavior

- Populates question field with: `"What did Chas and Dave discuss about {topic}? (Episode {ep_num})"`
- Adds `used` CSS class (dimmed styling, existing CSS)
- No auto-append behavior

### Question generation

Questions are generated client-side from label + episode number. The template `"What did Chas and Dave discuss about {topic}? (Episode {ep_num})"` leverages the existing query preprocessor which extracts episode filters from questions.

## What Gets Removed

- `extract_topics()` function
- `TOPIC_PROMPT` constant
- `TOPIC_MODEL` constant
- `anthropic` import in `topics.py`
- All Haiku-related post-filtering logic (replaced by `clean_episode_topics`)
- `topicPool` concept in frontend (replaced by episode-batched loading)

## What Stays

- `parse_description_topics()` — core HTML→labels parser, unchanged
- `_TIMESTAMP_RE`, `_SKIP_LABELS` constants — used by parser
- Segment/Unleashed/Cont. cleaning logic — consolidated into `clean_episode_topics()`
- `.topic-chip.used` and `.topic-chip.more` CSS — reused
- `not_ingested_episodes` computation — still live

## File Changes

- **Modify**: `src/pep_oracle/topics.py` — remove `extract_topics`, add `clean_episode_topics`, add `save_topics`/`load_topics` for file I/O
- **Modify**: `src/pep_oracle/ingest.py` — call topic extraction after ingestion
- **Modify**: `src/pep_oracle/server.py` — replace `_fetch_topics` with file read, update response shape
- **Modify**: `src/pep_oracle/web/index.html` — episode-batched chip rendering, inline episode numbers
- **Modify**: `tests/test_topics.py` — rewrite for new functions
- **Modify**: `tests/test_topics_endpoint.py` — update for new response shape
- **Modify**: `tests/test_web_episodes.py` — update Playwright tests for new chip format

## Migration

On first `/topics` request after upgrade, if `topics.json` doesn't exist, generate it from all feed episodes' descriptions (no ingestion needed — descriptions come from RSS). This is a one-time bootstrap using `parse_description_topics` + `clean_episode_topics` on each episode with a description.

## Testing

- `clean_episode_topics()`: segment stripping, Unleashed cleaning, Cont. removal, subtopic extraction
- `save_topics()`/`load_topics()`: file I/O, merge behavior, sort order
- Ingestion integration: topics file updated after ingestion
- `/topics` endpoint: correct response shape, file read, not_ingested_episodes
- Playwright: chip text includes episode number, "More..." loads next episode, click populates question with episode number
