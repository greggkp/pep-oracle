"""Per-endpoint server-side cache with TTL and background refresh."""

import time
from datetime import datetime, timezone


class CacheEntry:
    """Cache entry for a single endpoint's data."""

    def __init__(self, name: str, ttl_seconds: int):
        self.name = name
        self.ttl_seconds = ttl_seconds
        self.data = None
        self.updated_at: float | None = None  # time.monotonic() timestamp
        self.updated_at_iso: str | None = None  # ISO 8601 for API responses
        self.refreshing = False

    def is_stale(self) -> bool:
        if self.updated_at is None:
            return True
        return (time.monotonic() - self.updated_at) > self.ttl_seconds

    def set(self, data):
        self.data = data
        self.updated_at = time.monotonic()
        self.updated_at_iso = datetime.now(timezone.utc).isoformat()

    def invalidate(self):
        """Mark data as stale without clearing it."""
        self.updated_at = 0  # Forces is_stale() to return True

    def freshness(self) -> dict:
        return {
            "stale": self.is_stale() or self.refreshing,
            "updated_at": self.updated_at_iso,
        }


def get_freshness(entries: dict[str, CacheEntry]) -> dict:
    return {name: entry.freshness() for name, entry in entries.items()}
