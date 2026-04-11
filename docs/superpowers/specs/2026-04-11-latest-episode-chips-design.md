# Episode Chips: Prioritize Latest Episode — Design Spec

## Problem

Topic chips are extracted from the 5 most recent episodes equally, so chips often reference older episodes rather than the latest one. Users want chips to reflect what's in the newest episode.

## Solution

Change the `TOPIC_PROMPT` in `src/pep_oracle/topics.py` to instruct Haiku to extract as many topics as possible from the most recent episode first, only falling back to older episodes if the latest doesn't yield 5 topics.

## Prompt Change

Replace the current `TOPIC_PROMPT` with one that:
1. Explicitly marks the first episode as "LATEST"
2. Instructs Haiku to extract all topics from the latest episode first
3. Only use older episodes to fill remaining slots up to the target count
4. Keep existing requirements: dedup, `topic`/`question`/`episode_number` shape, recency words

The episodes list is already sorted newest-first by `extract_topics`, so the first entry is always the latest.

## No Code Logic Changes

- `extract_topics` function signature, sorting, filtering, and return type stay the same
- `server.py` and `index.html` are untouched
- The only change is the prompt text in `TOPIC_PROMPT`

## Test Changes

In `tests/test_topics.py`:
- Update `test_extract_topics_returns_parsed_topics` to verify the new prompt text includes the prioritization instruction (check the prompt sent to the mock client)
- No new tests needed — the behavior change is entirely in prompt wording, and the function's contract (input/output types, error handling) is unchanged

## Scope

- Modify: `src/pep_oracle/topics.py` (prompt text only)
- Modify: `tests/test_topics.py` (update prompt assertion in 1 test)
