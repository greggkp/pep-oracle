"""Versioned, immutable corpus artifact: vectors + text + metadata as parquet.

Layout under a local dir or s3:// base:
  <base>/corpus/vNNNN.parquet        # one row per chunk
  <base>/corpus/vNNNN.manifest.json  # provenance + sha256
  <base>/corpus/current.json         # the only mutable object: {version, sha256, manifest_url}

Publish is write-then-flip: parquet + manifest are written under immutable keys,
then current.json is overwritten LAST, so a reader sees old-or-new, never half.
The parquet columns (chunk_id, text, embedding, metadata-json) reload into the
exact dict shape ChromaDB's collection.get() returns, so retrieval code is reused.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import threading
import time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from pep_oracle import _storage as storage
from pep_oracle import config
from pep_oracle.config import CHROMA_COLLECTION
from pep_oracle.timing import timed


@dataclasses.dataclass
class Manifest:
    schema_ver: int
    embed_model: str
    dims: int
    episode_range: list  # [min, max] or [None, None]
    chunk_count: int
    ingest_git_sha: str
    built_at: str
    sha256: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _build_table(rows: list[dict]) -> pa.Table:
    return pa.table(
        {
            "chunk_id": pa.array([r["chunk_id"] for r in rows], pa.string()),
            "text": pa.array([r["text"] for r in rows], pa.string()),
            "embedding": pa.array([r["embedding"] for r in rows], pa.list_(pa.float32())),
            "metadata": pa.array([json.dumps(r["metadata"]) for r in rows], pa.string()),
        }
    )


def _table_bytes(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def _episode_range(rows: list[dict]) -> list:
    nums = sorted(n for r in rows if (n := r["metadata"].get("episode_number")))
    return [nums[0], nums[-1]] if nums else [None, None]


def write_artifact(
    rows: list[dict],
    *,
    dest: str,
    version: str,
    embed_model: str,
    dims: int,
    git_sha: str,
    built_at: str,
) -> Manifest:
    """Write vNNNN.parquet + manifest, then flip current.json. Returns the Manifest."""
    data = _table_bytes(_build_table(rows))
    sha = hashlib.sha256(data).hexdigest()
    manifest = Manifest(
        schema_ver=1,
        embed_model=embed_model,
        dims=dims,
        episode_range=_episode_range(rows),
        chunk_count=len(rows),
        ingest_git_sha=git_sha,
        built_at=built_at,
        sha256=sha,
    )

    base = str(dest).rstrip("/") + "/corpus"
    manifest_uri = f"{base}/{version}.manifest.json"
    storage.put_bytes(f"{base}/{version}.parquet", data)  # immutable
    storage.put_text(manifest_uri, json.dumps(manifest.to_dict(), indent=2))  # immutable
    storage.put_text(  # flip LAST
        f"{base}/current.json",
        json.dumps({"version": version, "sha256": sha, "manifest_url": manifest_uri}),
    )
    return manifest


class InMemoryCorpus:
    """In-memory stand-in for the slice of the ChromaDB Collection API that
    hybrid.hybrid_search and store.get_ingestion_stats use: `.name`, `.count()`,
    and `.get(include=[...])`. Backed by parallel lists loaded from the parquet
    artifact, so retrieval code is reused unchanged (no ChromaDB on this path)."""

    def __init__(self, ids, docs, embeddings, metas, version: str | None = None):
        self.name = CHROMA_COLLECTION
        self.version = version
        self.ids = ids
        self.docs = docs
        self.embeddings = embeddings
        self.metas = metas

    def count(self) -> int:
        return len(self.ids)

    def get(self, include=None) -> dict:
        include = include or []
        out = {"ids": list(self.ids)}
        if "documents" in include:
            out["documents"] = list(self.docs)
        if "embeddings" in include:
            # Returned as-is: a (N x dims) numpy matrix when loaded from parquet
            # (matching ChromaDB, which also returns an ndarray here). Copying into
            # a Python list would undo the cheap arrow→numpy load.
            out["embeddings"] = self.embeddings
        if "metadatas" in include:
            out["metadatas"] = list(self.metas)
        return out

    @classmethod
    def from_parquet_bytes(cls, data: bytes, version: str | None = None) -> InMemoryCorpus:
        # use_threads=False: arrow sizes its decode pool from os.cpu_count(), which on
        # Lambda reports the host's cores while the function only holds ~1 vCPU of
        # cgroup quota — the extra threads exhaust the quota early in each scheduling
        # period and stall (in-Lambda read_table measured 3.3s vs ~60ms local,
        # 2026-06-10, v1.2.2 sub-phase timing). Single-threaded is also faster locally
        # at this file size (~66ms vs ~158ms; thread coordination outweighs the gain).
        with timed("corpus.parse_read_table", bytes=len(data)):
            table = pq.read_table(io.BytesIO(data), use_threads=False)
        with timed("corpus.parse_columns", chunks=table.num_rows):
            ids = table.column("chunk_id").to_pylist()
            docs = table.column("text").to_pylist()
        # Load embeddings as one (N x dims) float32 numpy matrix, near-zero-copy from
        # arrow. The previous to_pylist() exploded the column into ~N*dims Python float
        # objects and dominated cold-start / refresh CPU (see docs cold-path measurement).
        with timed("corpus.parse_embeddings", chunks=table.num_rows):
            if table.num_rows:
                flat = table.column("embedding").combine_chunks().flatten()
                embeddings = (
                    flat.to_numpy(zero_copy_only=False)
                    .astype(np.float32, copy=False)
                    .reshape(table.num_rows, -1)
                )
            else:
                embeddings = np.zeros((0, 0), dtype=np.float32)
        with timed("corpus.parse_metadata", chunks=table.num_rows):
            metas = [json.loads(m) for m in table.column("metadata").to_pylist()]
        return cls(ids, docs, embeddings, metas, version=version)


def load_current(base: str) -> InMemoryCorpus:
    """Resolve <base>/corpus/current.json, download that version's parquet,
    verify sha256, and load it into an InMemoryCorpus."""
    prefix = str(base).rstrip("/") + "/corpus"
    cur = json.loads(storage.get_text(f"{prefix}/current.json"))
    version = cur["version"]
    with timed("corpus.download"):
        data = storage.get_bytes(f"{prefix}/{version}.parquet")
    actual = hashlib.sha256(data).hexdigest()
    if actual != cur["sha256"]:
        raise ValueError(
            f"corpus sha256 mismatch for {version}: current.json={cur['sha256']} actual={actual}"
        )
    with timed("corpus.parse", bytes=len(data)):
        return InMemoryCorpus.from_parquet_bytes(data, version=version)


def load_manifest(base: str) -> tuple[str, Manifest]:
    """Read <base>/corpus/current.json + the version's manifest. Returns (version, Manifest)."""
    prefix = str(base).rstrip("/") + "/corpus"
    cur = json.loads(storage.get_text(f"{prefix}/current.json"))
    version = cur["version"]
    m = json.loads(storage.get_text(f"{prefix}/{version}.manifest.json"))
    return version, Manifest(**m)


def _validate_serving(corpus: InMemoryCorpus, base: str) -> None:
    """Guard the serving path against a corpus/embedder mismatch:
      1. The manifest dims must match the loaded vectors' width.
      2. The active query embedder (config) must match the artifact's embed_model,
         else queries would be embedded in a different vector space than the corpus.
    Raises ValueError on either mismatch."""
    _version, manifest = load_manifest(base)
    if len(corpus.embeddings):
        actual_dims = len(corpus.embeddings[0])
        if actual_dims != manifest.dims:
            raise ValueError(
                f"corpus dims mismatch: manifest={manifest.dims} but vectors are {actual_dims}-d"
            )
    if manifest.embed_model != config.EMBED_MODEL:
        raise ValueError(
            f"query embedder mismatch: serving a {manifest.embed_model} corpus requires "
            f"EMBED_MODEL={manifest.embed_model}, but config has model={config.EMBED_MODEL!r}"
        )


# Process-cached serving corpus with bounded staleness. A warm process re-checks
# current.json every ttl_seconds (a cheap GET); on a version change it loads the new
# artifact and atomically swaps the reference (the read path never locks on the
# cached object). Combined with the (name, version)-keyed hybrid cache, the swap
# fully takes effect. One copy per process; concurrent Lambda containers each hold
# their own (S3 absorbs the fan-out).
_SERVING: dict = {"corpus": None, "version": None, "checked_at": 0.0}
_SERVING_LOCK = threading.Lock()


def reset_serving_cache() -> None:
    """Clear the process serving cache (tests + explicit reload)."""
    _SERVING.update(corpus=None, version=None, checked_at=0.0)


def current_corpus(base: str, ttl_seconds: int = 300, now=time.monotonic):
    """Return a process-cached InMemoryCorpus for <base>, refreshing on a TTL.

    Within ttl_seconds of the last check the cached corpus is returned without any
    I/O. After the TTL, current.json is re-read; if the version is unchanged the
    cached corpus is kept (TTL reset), otherwise the new version is loaded,
    validated (dims + embedder), and atomically swapped in."""
    cached = _SERVING["corpus"]
    t = now()
    if cached is not None and t - _SERVING["checked_at"] < ttl_seconds:
        return cached

    prefix = str(base).rstrip("/") + "/corpus"
    cur = json.loads(storage.get_text(f"{prefix}/current.json"))
    if cached is not None and cur["version"] == _SERVING["version"]:
        # Unchanged version: extend the TTL window. This single dict write is left
        # unlocked on purpose — it's a GIL-atomic update (like the read fast path),
        # and concurrent writers race only to set ~the same timestamp. Only the
        # corpus-swap below takes the lock.
        _SERVING["checked_at"] = t
        return cached

    with timed("corpus.load_and_validate"):
        fresh = load_current(base)
        _validate_serving(fresh, base)
    with _SERVING_LOCK:
        _SERVING["corpus"] = fresh
        _SERVING["version"] = fresh.version
        _SERVING["checked_at"] = t
    return fresh
