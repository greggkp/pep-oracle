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
