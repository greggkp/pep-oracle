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

import pyarrow as pa
import pyarrow.parquet as pq

from pep_oracle import _storage as storage
from pep_oracle.config import CHROMA_COLLECTION


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
            "embedding": pa.array(
                [r["embedding"] for r in rows], pa.list_(pa.float32())
            ),
            "metadata": pa.array(
                [json.dumps(r["metadata"]) for r in rows], pa.string()
            ),
        }
    )


def _table_bytes(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def _episode_range(rows: list[dict]) -> list:
    nums = sorted(
        n for r in rows if (n := r["metadata"].get("episode_number"))
    )
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
    storage.put_bytes(f"{base}/{version}.parquet", data)              # immutable
    storage.put_text(manifest_uri, json.dumps(manifest.to_dict(), indent=2))  # immutable
    storage.put_text(                                                # flip LAST
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
            out["embeddings"] = list(self.embeddings)
        if "metadatas" in include:
            out["metadatas"] = list(self.metas)
        return out

    @classmethod
    def from_parquet_bytes(cls, data: bytes, version: str | None = None) -> "InMemoryCorpus":
        table = pq.read_table(io.BytesIO(data))
        ids = table.column("chunk_id").to_pylist()
        docs = table.column("text").to_pylist()
        embeddings = table.column("embedding").to_pylist()
        metas = [json.loads(m) for m in table.column("metadata").to_pylist()]
        return cls(ids, docs, embeddings, metas, version=version)


def load_current(base: str) -> InMemoryCorpus:
    """Resolve <base>/corpus/current.json, download that version's parquet,
    verify sha256, and load it into an InMemoryCorpus."""
    prefix = str(base).rstrip("/") + "/corpus"
    cur = json.loads(storage.get_text(f"{prefix}/current.json"))
    version = cur["version"]
    data = storage.get_bytes(f"{prefix}/{version}.parquet")
    actual = hashlib.sha256(data).hexdigest()
    if actual != cur["sha256"]:
        raise ValueError(
            f"corpus sha256 mismatch for {version}: current.json={cur['sha256']} actual={actual}"
        )
    return InMemoryCorpus.from_parquet_bytes(data, version=version)
