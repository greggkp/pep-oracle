"""Lightweight phase timing for cold-path / latency observability.

Emits one structured log line per timed phase (``timing phase=<name> ms=<n>``)
so CloudWatch shows where a request — especially the first, cold one — spends
its time: the Bedrock embed round-trip, the S3 corpus download, the parquet
parse, the BM25 index build, etc. Zero-dependency and cheap (one perf_counter
pair + an INFO log per phase), so it is safe to leave on in prod.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

logger = logging.getLogger("pep_oracle.timing")


@contextmanager
def timed(phase: str, **fields):
    """Log the wall-clock duration of the wrapped block as one structured line.

    Extra keyword fields (e.g. ``bytes=...``, ``chunks=...``) are appended as
    ``key=value`` so a log query can break the cold path down by size as well as
    time. Always logs, even if the block raises.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - start) * 1000.0
        extra = "".join(f" {k}={v}" for k, v in fields.items())
        logger.info("timing phase=%s ms=%.1f%s", phase, ms, extra)
