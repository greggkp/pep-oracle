"""Tests for the server-side cache module."""

import time

from pep_oracle.cache import CacheEntry, get_freshness


def test_cache_entry_starts_empty():
    entry = CacheEntry(name="test", ttl_seconds=300)
    assert entry.data is None
    assert entry.updated_at is None
    assert entry.refreshing is False


def test_cache_entry_is_stale_when_empty():
    entry = CacheEntry(name="test", ttl_seconds=300)
    assert entry.is_stale() is True


def test_cache_entry_is_fresh_after_set():
    entry = CacheEntry(name="test", ttl_seconds=300)
    entry.set({"key": "value"})
    assert entry.data == {"key": "value"}
    assert entry.updated_at is not None
    assert entry.is_stale() is False


def test_cache_entry_is_stale_after_ttl():
    entry = CacheEntry(name="test", ttl_seconds=1)
    entry.set({"key": "value"})
    # Force the updated_at to be in the past
    entry.updated_at = entry.updated_at - 2
    assert entry.is_stale() is True


def test_cache_entry_invalidate():
    entry = CacheEntry(name="test", ttl_seconds=300)
    entry.set({"key": "value"})
    entry.invalidate()
    assert entry.is_stale() is True
    assert entry.data == {"key": "value"}  # data preserved until refresh


def test_cache_entry_freshness_dict_empty():
    entry = CacheEntry(name="test", ttl_seconds=300)
    result = entry.freshness()
    assert result == {"stale": True, "updated_at": None}


def test_cache_entry_freshness_dict_populated():
    entry = CacheEntry(name="test", ttl_seconds=300)
    entry.set({"key": "value"})
    result = entry.freshness()
    assert result["stale"] is False
    assert result["updated_at"] is not None


def test_get_freshness_returns_all_entries():
    entries = {
        "a": CacheEntry(name="a", ttl_seconds=300),
        "b": CacheEntry(name="b", ttl_seconds=300),
    }
    entries["a"].set({"x": 1})
    result = get_freshness(entries)
    assert result["a"]["stale"] is False
    assert result["b"]["stale"] is True


import asyncio  # noqa: E402
import pytest  # noqa: E402

from pep_oracle.cache import trigger_refresh  # noqa: E402


@pytest.mark.asyncio
async def test_trigger_refresh_populates_cache():
    entry = CacheEntry(name="test", ttl_seconds=300)
    call_count = 0

    def fetcher():
        nonlocal call_count
        call_count += 1
        return {"result": 42}

    await trigger_refresh(entry, fetcher)
    assert entry.data == {"result": 42}
    assert entry.is_stale() is False
    assert call_count == 1


@pytest.mark.asyncio
async def test_trigger_refresh_deduplicates():
    """Calling trigger_refresh while one is in progress should not start another."""
    entry = CacheEntry(name="test", ttl_seconds=300)
    call_count = 0

    def slow_fetcher():
        nonlocal call_count
        call_count += 1
        time.sleep(0.1)
        return {"result": call_count}

    # Fire two refreshes concurrently
    await asyncio.gather(
        trigger_refresh(entry, slow_fetcher),
        trigger_refresh(entry, slow_fetcher),
    )
    assert call_count == 1  # only one fetch happened


@pytest.mark.asyncio
async def test_trigger_refresh_preserves_data_on_error():
    entry = CacheEntry(name="test", ttl_seconds=300)
    entry.set({"old": True})

    def broken_fetcher():
        raise RuntimeError("fetch failed")

    await trigger_refresh(entry, broken_fetcher)
    assert entry.data == {"old": True}  # old data preserved
    assert entry.refreshing is False
