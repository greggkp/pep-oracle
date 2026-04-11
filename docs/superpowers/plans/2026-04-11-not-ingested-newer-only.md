# Not-Ingested: Newer Episodes Only — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Filter `not_ingested_episodes` to only show episodes newer than the most recently ingested episode, hiding old intentionally-skipped gaps.

**Architecture:** Add a 3-line filter in the `/topics` endpoint after computing the set difference. Update existing tests to match the new behavior and add one new test.

**Tech Stack:** FastAPI, pytest

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `src/pep_oracle/server.py:131` | Add newer-only filter after set difference |
| Modify | `tests/test_topics_endpoint.py:101-109` | Update existing test, add new test |

---

### Task 1: Update test for partial ingestion and add newer-only test

**Files:**
- Modify: `tests/test_topics_endpoint.py`

- [ ] **Step 1: Update `test_topics_some_episodes_ingested` to expect newer-only behavior**

In `tests/test_topics_endpoint.py`, replace the existing test at line 101-109:

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
```

With:

```python
def test_topics_some_episodes_ingested(client_and_collection):
    """With highest ingested ep=5, older gaps (2, 4) are excluded."""
    client, collection = client_and_collection
    _ingest(collection, 1)
    _ingest(collection, 3)
    _ingest(collection, 5)
    resp = client.get("/topics")
    data = resp.json()
    assert data["not_ingested_episodes"] == []
```

- [ ] **Step 2: Add new test for newer-only filtering**

Append after `test_topics_some_episodes_ingested`:

```python
def test_topics_only_newer_episodes_flagged(client_and_collection):
    """Only episodes newer than the highest ingested are flagged."""
    client, collection = client_and_collection
    _ingest(collection, 1)
    _ingest(collection, 2)
    _ingest(collection, 3)
    resp = client.get("/topics")
    data = resp.json()
    assert data["not_ingested_episodes"] == [4, 5]
```

- [ ] **Step 3: Run tests to verify the updated test fails**

Run: `uv run pytest tests/test_topics_endpoint.py::test_topics_some_episodes_ingested tests/test_topics_endpoint.py::test_topics_only_newer_episodes_flagged -v`
Expected: `test_topics_some_episodes_ingested` FAILS (returns `[2, 4]` instead of `[]`), `test_topics_only_newer_episodes_flagged` FAILS (returns `[4, 5]` plus older gaps).

---

### Task 2: Add newer-only filter to server endpoint

**Files:**
- Modify: `src/pep_oracle/server.py:131`

- [ ] **Step 1: Add the filter**

In `src/pep_oracle/server.py`, replace line 131:

```python
        not_ingested = sorted(feed_eps - ingested_eps)
```

With:

```python
        not_ingested = sorted(feed_eps - ingested_eps)
        # Only flag episodes newer than the most recently ingested
        if ingested_eps:
            latest_ingested = max(ingested_eps)
            not_ingested = [ep for ep in not_ingested if ep > latest_ingested]
```

- [ ] **Step 2: Run all endpoint tests**

Run: `uv run pytest tests/test_topics_endpoint.py -v`
Expected: All 9 tests PASS (7 existing + 1 updated + 1 new).

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest -x -q`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/pep_oracle/server.py tests/test_topics_endpoint.py
git commit -m "feat: filter not-ingested episodes to only show newer than latest ingested"
```
