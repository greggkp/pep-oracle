# Not-Ingested Episodes: Show Only Newer — Design Spec

## Problem

The not-ingested episode detection flags ALL un-ingested episodes from the RSS feed, including old episodes that were intentionally skipped (e.g., episodes 169-219). This clutters the UI with irrelevant episodes. Users only care about NEW episodes that haven't been ingested yet.

## Solution

Filter `not_ingested_episodes` to only include episodes with an episode number greater than the highest ingested episode number. If no episodes are ingested, show all (preserving current behavior for fresh installs).

## Server Change

In `src/pep_oracle/server.py`, the `/topics` endpoint, after computing `not_ingested = sorted(feed_eps - ingested_eps)`:

```python
if ingested_eps:
    latest_ingested = max(ingested_eps)
    not_ingested = [ep for ep in not_ingested if ep > latest_ingested]
```

## Test Changes

In `tests/test_topics_endpoint.py`:

- **Update** `test_topics_some_episodes_ingested`: With episodes 1, 3, 5 ingested (max=5), episodes 2 and 4 are older than 5, so `not_ingested_episodes` should be `[]`.
- **Update** `test_topics_no_episodes_ingested`: No change needed — still returns `[1, 2, 3, 4, 5]` since there are no ingested episodes to filter against.
- **Add** `test_topics_only_newer_episodes_flagged`: Ingest episodes 1-3 (max=3), feed has 1-5. Expected: `[4, 5]`.

Existing Playwright tests should pass without changes — the fixture's topic episodes (3 and 5) and ingestion patterns are compatible with the new filtering.

## No Frontend Changes

The JavaScript already handles whatever list the server returns.

## Scope

- Modify: `src/pep_oracle/server.py` (~3 lines added)
- Modify: `tests/test_topics_endpoint.py` (1 test updated, 1 test added)
