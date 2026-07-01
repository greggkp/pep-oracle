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
import logging
import struct
import threading
import time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from pep_oracle import _storage as storage
from pep_oracle import config
from pep_oracle.config import CHROMA_COLLECTION
from pep_oracle.lexical import BM25, build_bm25
from pep_oracle.timing import timed

log = logging.getLogger(__name__)

# Self-describing frame for the prebuilt BM25 index sidecar (corpus/vNNNN.bm25.zst):
#   8-byte MAGIC + uint64-LE uncompressed-length + zstd(json of BM25 state).
# The MAGIC lets a reader reject a wrong/garbage object before decompressing; the
# length is required by pa.decompress for zstd (the raw frame carries no size).
_INDEX_MAGIC = b"PEPBM25\x00"

# Frame for the prebuilt embedding-matrix sidecar (corpus/vNNNN.emb.zst):
#   8-byte MAGIC + <QII> (raw byte length, rows, dims) + 64-byte ascii parquet
#   sha256 hex + zstd(row-major float32 matrix bytes).
# Same provenance idea as the BM25 sidecar: the embedded sha proves the matrix
# was built from the exact parquet being served, so the serving path can skip
# decoding the parquet's embedding column (the bulk of its decode cost).
_EMB_MAGIC = b"PEPEMBF\x00"
_EMB_HEADER_LEN = 8 + struct.calcsize("<QII") + 64


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


def _serialize_index(rows: list[dict], parquet_sha: str) -> bytes:
    """Build the BM25 index over the rows' text and pack it as the sidecar frame.
    Embeds the parquet sha256 + chunk_count so the loader can PROVE the index was
    built from the exact parquet it is serving (and reject a stale one)."""
    state = build_bm25([r["text"] for r in rows]).to_dict()
    state["parquet_sha256"] = parquet_sha
    state["chunk_count"] = len(rows)
    jb = json.dumps(state).encode("utf-8")
    comp = pa.compress(jb, codec="zstd").to_pybytes()
    return _INDEX_MAGIC + struct.pack("<Q", len(jb)) + comp


def _deserialize_index(data: bytes, *, parquet_sha: str, chunk_count: int) -> BM25 | None:
    """Inverse of `_serialize_index`, with provenance validation. Returns a ready
    BM25, or None if the bytes are not a valid index for THIS parquet (caller then
    rebuilds). Raises only on genuinely corrupt frames (caught one level up)."""
    if not data.startswith(_INDEX_MAGIC):
        return None
    (n,) = struct.unpack("<Q", data[8:16])
    jb = pa.decompress(data[16:], decompressed_size=n, codec="zstd", asbytes=True)
    state = json.loads(jb)
    # Provenance coupling: the index must have been built from this exact parquet.
    if state.get("parquet_sha256") != parquet_sha or state.get("chunk_count") != chunk_count:
        return None
    return BM25.from_dict(state, expected_n=chunk_count)


def _serialize_embeddings(rows: list[dict], parquet_sha: str) -> bytes:
    """Pack the rows' embeddings as a raw float32 matrix in the sidecar frame.
    Embeds the parquet sha256 so the loader can PROVE the matrix came from the
    exact parquet it is serving (a stale one would silently mis-rank everything)."""
    mat = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
    if mat.ndim != 2:
        raise ValueError(f"embeddings are not a uniform matrix (shape {mat.shape})")
    raw = mat.tobytes(order="C")
    comp = pa.compress(raw, codec="zstd").to_pybytes()
    header = struct.pack("<QII", len(raw), mat.shape[0], mat.shape[1])
    return _EMB_MAGIC + header + parquet_sha.encode("ascii") + comp


def _deserialize_embeddings(data: bytes, *, parquet_sha: str) -> np.ndarray | None:
    """Inverse of `_serialize_embeddings`, with provenance validation. Returns the
    (N x dims) float32 matrix (read-only view — the serving path never writes it),
    or None if the bytes are not a valid matrix for THIS parquet (caller then falls
    back to the parquet's embedding column)."""
    if not data.startswith(_EMB_MAGIC) or len(data) < _EMB_HEADER_LEN:
        return None
    raw_len, rows, dims = struct.unpack("<QII", data[8 : _EMB_HEADER_LEN - 64])
    sha = data[_EMB_HEADER_LEN - 64 : _EMB_HEADER_LEN].decode("ascii", errors="replace")
    if sha != parquet_sha or raw_len != rows * dims * 4:
        return None
    raw = pa.decompress(
        data[_EMB_HEADER_LEN:], decompressed_size=raw_len, codec="zstd", asbytes=True
    )
    if len(raw) != raw_len:
        return None
    return np.frombuffer(raw, dtype=np.float32).reshape(rows, dims)


def _load_prebuilt_embeddings(prefix: str, version: str, *, parquet_sha: str) -> np.ndarray | None:
    """Fetch + validate the prebuilt embedding-matrix sidecar for a corpus version.
    Returns the matrix, or None on ANY doubt (absent from an older artifact, stale,
    corrupt) — the parse path then decodes the parquet's embedding column as before.
    Correctness over latency: a wrong matrix must never silently mis-rank."""
    try:
        with timed("corpus.emb_download"):
            data = storage.get_bytes(f"{prefix}/{version}.emb.zst")
    except Exception:  # noqa: BLE001 — absent sidecar (pre-sidecar artifact) → parquet path
        return None
    try:
        with timed("corpus.emb_decode", bytes=len(data)):
            mat = _deserialize_embeddings(data, parquet_sha=parquet_sha)
        if mat is None:
            log.warning(
                "prebuilt embeddings for %s rejected (stale/mismatch); using parquet column",
                version,
            )
        return mat
    except Exception:  # noqa: BLE001 — corrupt/garbage sidecar → parquet path
        log.warning(
            "prebuilt embeddings for %s failed to decode; using parquet column",
            version,
            exc_info=True,
        )
        return None


def _load_prebuilt_index(
    prefix: str, version: str, *, parquet_sha: str, chunk_count: int
) -> BM25 | None:
    """Fetch + validate the prebuilt BM25 sidecar for a corpus version. Returns the
    index, or None on ANY doubt (absent sidecar from an older artifact, stale,
    corrupt, or built by a different code version) — the serving path then rebuilds.
    Correctness over latency: a wrong index must never silently mis-score."""
    if chunk_count == 0:
        return None
    try:
        with timed("corpus.index_download"):
            data = storage.get_bytes(f"{prefix}/{version}.bm25.zst")
    except Exception:  # noqa: BLE001 — absent sidecar (pre-index artifact) → rebuild
        return None
    try:
        with timed("corpus.index_decode", chunks=chunk_count):
            idx = _deserialize_index(data, parquet_sha=parquet_sha, chunk_count=chunk_count)
        if idx is None:
            log.warning("prebuilt bm25 index for %s rejected (stale/mismatch); rebuilding", version)
        return idx
    except Exception:  # noqa: BLE001 — corrupt/garbage sidecar → rebuild
        log.warning(
            "prebuilt bm25 index for %s failed to decode; rebuilding", version, exc_info=True
        )
        return None


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
    """Write vNNNN.parquet + manifest (+ prebuilt BM25 sidecar), then flip
    current.json. Returns the Manifest."""
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
    if rows:
        # Prebuilt lexical index — a pure serving-latency optimization. Write it
        # under the immutable version key BEFORE the current.json flip, but never
        # let a serialize/upload failure block the publish (serving rebuilds).
        try:
            storage.put_bytes(f"{base}/{version}.bm25.zst", _serialize_index(rows, sha))
        except Exception:  # noqa: BLE001
            log.warning(
                "failed to write prebuilt bm25 index for %s; serving will rebuild",
                version,
                exc_info=True,
            )
        # Prebuilt embedding matrix — same contract: immutable, pre-flip, and a
        # failure never blocks the publish (serving decodes the parquet column).
        try:
            storage.put_bytes(f"{base}/{version}.emb.zst", _serialize_embeddings(rows, sha))
        except Exception:  # noqa: BLE001
            log.warning(
                "failed to write prebuilt embeddings for %s; serving will use the parquet column",
                version,
                exc_info=True,
            )
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

    def __init__(
        self, ids, docs, embeddings, metas, version: str | None = None, prebuilt_bm25=None
    ):
        self.name = CHROMA_COLLECTION
        self.version = version
        self.ids = ids
        self.docs = docs
        self.embeddings = embeddings
        self.metas = metas
        # Optional prebuilt BM25 index (from the artifact sidecar); hybrid adopts it
        # to skip the cold-start rebuild. None → hybrid builds from docs as before.
        self.prebuilt_bm25 = prebuilt_bm25

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
    def from_parquet_bytes(
        cls,
        data: bytes,
        version: str | None = None,
        prebuilt_embeddings: np.ndarray | None = None,
    ) -> InMemoryCorpus:
        # use_threads=False: arrow sizes its decode pool from os.cpu_count(), which on
        # Lambda reports the host's cores while the function only holds ~1 vCPU of
        # cgroup quota — the extra threads exhaust the quota early in each scheduling
        # period and stall (in-Lambda read_table measured 3.3s vs ~60ms local,
        # 2026-06-10, v1.2.2 sub-phase timing). Single-threaded is also faster locally
        # at this file size (~66ms vs ~158ms; thread coordination outweighs the gain).
        #
        # With a validated prebuilt embedding matrix (the .emb.zst sidecar), skip the
        # embedding column entirely — it dominates the parquet's decode cost. Parquet
        # is columnar, so a column subset never touches the skipped column's pages.
        want = ["chunk_id", "text", "metadata"] if prebuilt_embeddings is not None else None
        with timed("corpus.parse_read_table", bytes=len(data)):
            table = pq.read_table(io.BytesIO(data), use_threads=False, columns=want)
        with timed("corpus.parse_columns", chunks=table.num_rows):
            ids = table.column("chunk_id").to_pylist()
            docs = table.column("text").to_pylist()
        if prebuilt_embeddings is not None and len(prebuilt_embeddings) != table.num_rows:
            # Belt-and-braces on top of the sidecar's sha256 provenance check: a
            # matrix of the wrong height can never be served. Fall back to the column.
            log.warning(
                "prebuilt embeddings row count %d != parquet rows %d; using parquet column",
                len(prebuilt_embeddings),
                table.num_rows,
            )
            prebuilt_embeddings = None
        if prebuilt_embeddings is not None:
            embeddings = prebuilt_embeddings
        else:
            # Load embeddings as one (N x dims) float32 numpy matrix, near-zero-copy
            # from arrow. The previous to_pylist() exploded the column into ~N*dims
            # Python float objects and dominated cold-start / refresh CPU (see docs
            # cold-path measurement). With the sidecar shipped this branch firing on
            # a non-empty corpus is an ops signal (sidecar absent/rejected).
            with timed("corpus.parse_embeddings", chunks=table.num_rows):
                if table.num_rows:
                    col = (
                        table.column("embedding")
                        if "embedding" in table.column_names
                        else pq.read_table(
                            io.BytesIO(data), use_threads=False, columns=["embedding"]
                        ).column("embedding")
                    )
                    flat = col.combine_chunks().flatten()
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
    # Fetch the prebuilt embedding matrix BEFORE parsing: with a valid sidecar the
    # parse skips the parquet's embedding column (its dominant decode cost). The
    # parquet sha is already verified above, so cur["sha256"] proves provenance.
    emb = _load_prebuilt_embeddings(prefix, version, parquet_sha=cur["sha256"])
    with timed("corpus.parse", bytes=len(data)):
        corpus = InMemoryCorpus.from_parquet_bytes(data, version=version, prebuilt_embeddings=emb)
    # Attach the prebuilt BM25 index if the artifact ships one.
    corpus.prebuilt_bm25 = _load_prebuilt_index(
        prefix, version, parquet_sha=cur["sha256"], chunk_count=corpus.count()
    )
    return corpus


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
