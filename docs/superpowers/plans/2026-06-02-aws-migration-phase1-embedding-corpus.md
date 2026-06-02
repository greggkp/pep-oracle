# AWS Migration Phase 1 — Bedrock Embedding + Corpus Artifact + Backfill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 1.3 GB local `bge-large` embedder with an AWS Bedrock embedding backend, define a versioned flat corpus artifact (parquet + manifest + `current.json`) with an in-memory loader that is a drop-in for the existing retrieval layer, add a one-time backfill command that re-embeds the 95 existing episodes, and prove via the eval harness that Titan-v2 retrieval quality holds vs `bge-large`.

**Architecture:** `embeddings.embed_texts()` keeps its signature but gains a pluggable backend selected by `PEP_ORACLE_EMBED_BACKEND` (`fastembed` default, `bedrock` opt-in via Bedrock `amazon.titan-embed-text-v2:0` in `ap-southeast-2`). A new `corpus.py` writes/reads an immutable artifact (`vNNNN.parquet` + `vNNNN.manifest.json` + mutable `current.json`) to a local dir **or** `s3://` URI via a thin `_storage.py` dispatch. `InMemoryCorpus` loads the parquet into a structure exposing `.name`/`.count()`/`.get(include=...)` — byte-compatible with the slice of the ChromaDB `Collection` API that `hybrid.hybrid_search` and `store.get_ingestion_stats` consume, so the retrieval code is reused unchanged. `backfill.py` reads the `pep-oracle export` JSON, re-embeds, and publishes `v0001`. The eval harness gains `evaluate_corpus()` so retrieval quality is measured over an artifact and compared to the live `bge-large` ChromaDB baseline.

**Tech Stack:** Python 3.11, boto3 (Bedrock + S3), pyarrow (parquet), click (CLI), pytest. No ChromaDB on the artifact path. Region `ap-southeast-2` (Sydney) — Bedrock Titan v2 is not in Melbourne/`ap-southeast-4`.

**Scope boundary:** This phase produces a *locally runnable and testable* embedding+artifact pipeline. It does **not** touch Lambda, FastAPI/Mangum, OAuth/DynamoDB, Cognito, CloudFront, or CDK — those are Phase 2. The serving wiring that *consumes* `InMemoryCorpus` (the `/version` endpoint, the TTL refresh of `current.json`) is Phase 2. Actual prod S3 bucket creation is Phase 2/3 CDK; this phase only needs the S3 read/write *helpers* (unit-tested with a monkeypatched client) so `--out s3://...` works once a bucket exists.

---

## File Structure

**Created:**
- `src/pep_oracle/_storage.py` — local-path vs `s3://` byte/text put+get dispatch (lazy boto3 S3 client).
- `src/pep_oracle/corpus.py` — `Manifest` dataclass, `write_artifact()`, `InMemoryCorpus`, `load_current()`, sha256 helpers.
- `src/pep_oracle/backfill.py` — `backfill()`: export JSON → re-embed → publish `vNNNN`.
- `tests/test_storage.py`, `tests/test_corpus.py`, `tests/test_backfill.py`
- `tests/fixtures/export_sample.json` — tiny exported-chunk fixture for backfill tests.
- `docs/aws/phase1-backfill-runbook.md` — operator steps for the one-time migration (needs AWS creds).

**Modified:**
- `pyproject.toml` — add `aws` optional extra (`boto3`, `pyarrow`); add both to the `dev` dependency-group so `uv run pytest` has them.
- `src/pep_oracle/config.py` — embedding/region/corpus env knobs.
- `src/pep_oracle/embeddings.py` — pluggable backend (fastembed | bedrock) behind the unchanged `embed_texts()`.
- `src/pep_oracle/eval_retrieval.py` — `_hybrid_retriever(collection, embed=None)`, `evaluate_corpus()`, `format_single()`.
- `src/pep_oracle/cli.py` — new `backfill` command; `eval-retrieval` gains `--corpus`.
- `tests/test_embeddings.py` — Bedrock-backend tests (monkeypatched boto3).
- `tests/test_eval_retrieval.py` — `evaluate_corpus` mechanics test.
- `CLAUDE.md` — short note on the Bedrock backend, corpus artifact, and backfill command.

---

## Task 1: Dependencies & config knobs

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/pep_oracle/config.py`

- [ ] **Step 1: Add the `aws` extra and dev deps in `pyproject.toml`**

In `[project.optional-dependencies]`, add an `aws` extra alongside the existing `server` line:

```toml
[project.optional-dependencies]
server = ["fastapi", "uvicorn[standard]", "mcp>=1.0", "pyjwt[crypto]>=2.8"]
aws = ["boto3>=1.34", "pyarrow>=15"]
```

In `[dependency-groups]`, add `boto3` and `pyarrow` so the test venv has them:

```toml
[dependency-groups]
dev = [
    "pytest-asyncio>=1.3.0",
    "boto3>=1.34",
    "pyarrow>=15",
]
```

- [ ] **Step 2: Add config knobs in `src/pep_oracle/config.py`**

After the existing `CHROMA_COLLECTION` / `QUERY_MODEL` lines (around line 22-23), add:

```python
# --- Embedding backend (fastembed local | AWS Bedrock) ---
# Default stays "fastembed" so existing local ingestion/CLI/tests are unchanged;
# the AWS migration opts in with PEP_ORACLE_EMBED_BACKEND=bedrock.
EMBED_BACKEND = os.getenv("PEP_ORACLE_EMBED_BACKEND", "fastembed")
# Sydney — operator default; Bedrock Titan v2 isn't in ap-southeast-4 (Melbourne).
BEDROCK_REGION = os.getenv("PEP_ORACLE_BEDROCK_REGION", "ap-southeast-2")
# EMBED_MODEL / EMBED_DIMS apply when EMBED_BACKEND=bedrock (the fastembed model
# name lives in embeddings.MODEL_NAME).
EMBED_MODEL = os.getenv("PEP_ORACLE_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
EMBED_DIMS = int(os.getenv("PEP_ORACLE_EMBED_DIMS", "1024"))

# --- Corpus artifact base location (local dir or s3:// base URI) ---
# The artifact lives under <CORPUS_URI>/corpus/{vNNNN.parquet,vNNNN.manifest.json,current.json};
# the "/corpus" prefix is appended by corpus.py, so this is the BASE, not the corpus dir itself.
CORPUS_URI = os.getenv("PEP_ORACLE_CORPUS_URI", str(DATA_DIR))
```

- [ ] **Step 3: Install and smoke-check the import**

Run:
```bash
uv pip install -e ".[aws]"
uv run python -c "import boto3, pyarrow; from pep_oracle import config; print(config.EMBED_BACKEND, config.BEDROCK_REGION, config.EMBED_MODEL, config.EMBED_DIMS)"
```
Expected: `fastembed ap-southeast-2 amazon.titan-embed-text-v2:0 1024`

- [ ] **Step 4: Confirm the suite still passes**

Run: `uv run pytest -q`
Expected: PASS (no behavior changed yet; boto3/pyarrow now importable).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/pep_oracle/config.py
git commit -m "feat(aws): add boto3/pyarrow deps and Bedrock/corpus config knobs"
```

---

## Task 2: Bedrock embedding backend

**Files:**
- Modify: `src/pep_oracle/embeddings.py`
- Test: `tests/test_embeddings.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_embeddings.py`:

```python
import json

import pytest

import pep_oracle.embeddings as embeddings


class _FakeBody:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()


class _FakeBedrock:
    """Records invoke_model calls and returns a deterministic embedding."""

    def __init__(self):
        self.calls = []

    def invoke_model(self, *, modelId, body):
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
        text = json.loads(body)["inputText"]
        # 1024-d vector seeded by text length so distinct inputs differ.
        return {"body": _FakeBody({"embedding": [float(len(text))] * 1024})}


def test_bedrock_backend_calls_invoke_model_with_titan_body(monkeypatch):
    fake = _FakeBedrock()
    monkeypatch.setattr(embeddings, "_bedrock_client", lambda: fake)
    monkeypatch.setattr(embeddings.config, "EMBED_BACKEND", "bedrock")

    out = embeddings.embed_texts(["hello", "hello world"])

    assert len(out) == 2
    assert len(out[0]) == 1024
    assert out[0] != out[1]  # distinct inputs -> distinct vectors
    # Titan v2 invoke body: inputText + dimensions + normalize
    assert fake.calls[0]["modelId"] == "amazon.titan-embed-text-v2:0"
    assert fake.calls[0]["body"] == {"inputText": "hello", "dimensions": 1024, "normalize": True}


def test_bedrock_backend_retries_on_throttling(monkeypatch):
    attempts = {"n": 0}

    class _Throttler:
        def invoke_model(self, *, modelId, body):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise embeddings._ThrottlingError("slow down")
            return {"body": _FakeBody({"embedding": [0.1] * 1024})}

    monkeypatch.setattr(embeddings, "_bedrock_client", lambda: _Throttler())
    monkeypatch.setattr(embeddings.config, "EMBED_BACKEND", "bedrock")
    monkeypatch.setattr(embeddings.time, "sleep", lambda _s: None)  # no real backoff wait

    out = embeddings.embed_texts(["x"])

    assert attempts["n"] == 3  # two failures, third succeeds
    assert len(out[0]) == 1024
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_embeddings.py -k bedrock -v`
Expected: FAIL (`embeddings` has no `_bedrock_client`, `config`, `time`, or `_ThrottlingError`).

- [ ] **Step 3: Implement the backend**

Replace the entire contents of `src/pep_oracle/embeddings.py` with:

```python
"""Text embedding with a pluggable backend.

`embed_texts()` keeps a single public signature (list[str] -> list[list[float]]).
The backend is selected by config.EMBED_BACKEND:
  - "fastembed": local BAAI/bge-large-en-v1.5 (1024-d) — the original path.
  - "bedrock":   AWS Bedrock amazon.titan-embed-text-v2:0 (configurable dims),
                 used by the AWS migration. One InvokeModel call per text.
Query and corpus vectors must come from the SAME backend+model (one vector
space) — see corpus manifest `embed_model`.
"""

from __future__ import annotations

import json
import time

from pep_oracle import config

MODEL_NAME = "BAAI/bge-large-en-v1.5"

_model = None        # fastembed singleton
_bedrock = None      # boto3 bedrock-runtime singleton

_MAX_RETRIES = 6
_BASE_BACKOFF = 0.5  # seconds; doubled each retry


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(MODEL_NAME)
    return _model


def _bedrock_client():
    global _bedrock
    if _bedrock is None:
        import boto3

        _bedrock = boto3.client("bedrock-runtime", region_name=config.BEDROCK_REGION)
    return _bedrock


class _ThrottlingError(Exception):
    """Internal marker so the retry loop is testable without importing botocore."""


def _is_throttling(exc: Exception) -> bool:
    if isinstance(exc, _ThrottlingError):
        return True
    name = exc.__class__.__name__
    return name in {"ThrottlingException", "TooManyRequestsException", "ModelTimeoutException"}


def _embed_one_bedrock(text: str) -> list[float]:
    body = json.dumps(
        {"inputText": text, "dimensions": config.EMBED_DIMS, "normalize": True}
    )
    for attempt in range(_MAX_RETRIES):
        try:
            resp = _bedrock_client().invoke_model(modelId=config.EMBED_MODEL, body=body)
            return json.loads(resp["body"].read())["embedding"]
        except Exception as exc:  # noqa: BLE001 — retry only throttling, re-raise the rest
            if _is_throttling(exc) and attempt < _MAX_RETRIES - 1:
                time.sleep(_BASE_BACKOFF * (2 ** attempt))
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def embed_texts(texts: list[str]) -> list[list[float]]:
    if config.EMBED_BACKEND == "bedrock":
        return [_embed_one_bedrock(t) for t in texts]
    return [v.tolist() for v in _get_model().embed(texts)]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_embeddings.py -k bedrock -v`
Expected: PASS (both bedrock tests).

- [ ] **Step 5: Confirm the existing fastembed test still passes**

Run: `uv run pytest tests/test_embeddings.py -v`
Expected: PASS — including `test_embed_texts_returns_expected_shape_and_distinct_vectors` (default backend unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/pep_oracle/embeddings.py tests/test_embeddings.py
git commit -m "feat(aws): pluggable embedding backend with Bedrock Titan v2 path"
```

---

## Task 3: Storage helper (local + s3 dispatch)

**Files:**
- Create: `src/pep_oracle/_storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_storage.py`:

```python
import pep_oracle._storage as storage


def test_local_roundtrip_bytes_and_text(tmp_path):
    p = tmp_path / "sub" / "blob.bin"
    storage.put_bytes(str(p), b"\x00\x01\x02")
    assert storage.get_bytes(str(p)) == b"\x00\x01\x02"  # parent dir auto-created

    t = tmp_path / "sub" / "doc.json"
    storage.put_text(str(t), '{"a": 1}')
    assert storage.get_text(str(t)) == '{"a": 1}'


def test_is_s3():
    assert storage.is_s3("s3://bucket/key")
    assert not storage.is_s3("/local/path")


class _FakeS3:
    def __init__(self):
        self.store = {}

    def put_object(self, *, Bucket, Key, Body):
        self.store[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):
        data = self.store[(Bucket, Key)]

        class _Body:
            def read(self_inner):
                return data

        return {"Body": _Body()}


def test_s3_roundtrip(monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(storage, "_s3", lambda: fake)

    storage.put_bytes("s3://corpus/corpus/v0001.parquet", b"PARQUET")
    assert ("corpus", "corpus/v0001.parquet") in fake.store
    assert storage.get_bytes("s3://corpus/corpus/v0001.parquet") == b"PARQUET"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_storage.py -v`
Expected: FAIL (`No module named 'pep_oracle._storage'`).

- [ ] **Step 3: Implement `src/pep_oracle/_storage.py`**

```python
"""Tiny storage dispatch: a URI is either a local filesystem path or s3://bucket/key.

Keeps corpus read/write code agnostic to where the artifact lives (local dev dir
vs S3 in prod). The S3 client is lazy so non-AWS installs never import boto3.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from pep_oracle import config

_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        import boto3

        _s3_client = boto3.client("s3", region_name=config.BEDROCK_REGION)
    return _s3_client


def is_s3(uri: str) -> bool:
    return str(uri).startswith("s3://")


def _split_s3(uri: str) -> tuple[str, str]:
    parts = urlparse(str(uri))
    return parts.netloc, parts.path.lstrip("/")


def put_bytes(uri: str, data: bytes) -> None:
    if is_s3(uri):
        bucket, key = _split_s3(uri)
        _s3().put_object(Bucket=bucket, Key=key, Body=data)
    else:
        p = Path(uri)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def get_bytes(uri: str) -> bytes:
    if is_s3(uri):
        bucket, key = _split_s3(uri)
        return _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    return Path(uri).read_bytes()


def put_text(uri: str, text: str) -> None:
    put_bytes(uri, text.encode("utf-8"))


def get_text(uri: str) -> str:
    return get_bytes(uri).decode("utf-8")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_storage.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/_storage.py tests/test_storage.py
git commit -m "feat(aws): local/s3 storage dispatch helper"
```

---

## Task 4: Corpus artifact writer + Manifest

**Files:**
- Create: `src/pep_oracle/corpus.py`
- Test: `tests/test_corpus.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_corpus.py`:

```python
import hashlib
import json

import pep_oracle.corpus as corpus


def _row(cid, text, ep, embedding):
    return {
        "chunk_id": cid,
        "text": text,
        "embedding": embedding,
        "metadata": {
            "episode_guid": f"g{ep}",
            "episode_title": f"Ep {ep}",
            "episode_date": "2026-01-01",
            "episode_number": ep,
            "start_time": 0.0,
            "end_time": 10.0,
        },
    }


def test_write_artifact_emits_parquet_manifest_and_current(tmp_path):
    rows = [
        _row("a", "byrd rule reconciliation", 251, [0.1, 0.2]),
        _row("b", "tariffs section 122", 253, [0.3, 0.4]),
    ]
    manifest = corpus.write_artifact(
        rows,
        dest=str(tmp_path),
        version="v0001",
        embed_model="amazon.titan-embed-text-v2:0",
        dims=2,
        git_sha="abc1234",
        built_at="2026-06-02T00:00:00+00:00",
    )

    base = tmp_path / "corpus"
    assert (base / "v0001.parquet").exists()
    assert (base / "v0001.manifest.json").exists()
    assert (base / "current.json").exists()

    # Manifest content
    assert manifest.chunk_count == 2
    assert manifest.episode_range == [251, 253]
    assert manifest.embed_model == "amazon.titan-embed-text-v2:0"
    assert manifest.dims == 2

    # current.json points at the version and matches the parquet sha256
    cur = json.loads((base / "current.json").read_text())
    assert cur["version"] == "v0001"
    parquet_sha = hashlib.sha256((base / "v0001.parquet").read_bytes()).hexdigest()
    assert cur["sha256"] == parquet_sha == manifest.sha256


def test_write_artifact_handles_missing_episode_numbers(tmp_path):
    rows = [_row("a", "x", 0, [0.1, 0.2])]  # 0 == store sentinel for "no episode"
    rows[0]["metadata"]["episode_number"] = 0
    manifest = corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="m", dims=2, git_sha="s", built_at="t",
    )
    assert manifest.episode_range == [None, None]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_corpus.py -v`
Expected: FAIL (`No module named 'pep_oracle.corpus'`).

- [ ] **Step 3: Implement the writer half of `src/pep_oracle/corpus.py`**

Create `src/pep_oracle/corpus.py` with the writer (the loader is added in Task 5):

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_corpus.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/corpus.py tests/test_corpus.py
git commit -m "feat(aws): corpus artifact writer (parquet + manifest + current.json)"
```

---

## Task 5: InMemoryCorpus loader + drop-in retrieval

**Files:**
- Modify: `src/pep_oracle/corpus.py`
- Test: `tests/test_corpus.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_corpus.py`:

```python
from pep_oracle.hybrid import hybrid_search


def test_inmemory_corpus_roundtrip_and_get_shape(tmp_path):
    rows = [
        _row("a", "byrd rule reconciliation", 251, [1.0, 0.0]),
        _row("b", "weather and chit chat", 252, [0.0, 1.0]),
    ]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="m", dims=2, git_sha="s", built_at="t",
    )

    c = corpus.load_current(str(tmp_path))
    assert c.count() == 2
    assert c.name == "pep_oracle"

    got = c.get(include=["documents", "embeddings", "metadatas"])
    assert got["ids"] == ["a", "b"]
    assert got["documents"][0] == "byrd rule reconciliation"
    assert got["embeddings"][0] == [1.0, 0.0]
    assert got["metadatas"][0]["episode_number"] == 251
    # include is honored: omit a key -> absent
    assert "documents" not in c.get(include=["metadatas"])


def test_inmemory_corpus_is_drop_in_for_hybrid_search(tmp_path):
    rows = [
        _row("a", "byrd rule reconciliation senate", 251, [1.0, 0.0]),
        _row("b", "weather and chit chat", 252, [0.0, 1.0]),
    ]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="m", dims=2, git_sha="s", built_at="t",
    )
    c = corpus.load_current(str(tmp_path))

    results = hybrid_search(c, "byrd rule", [1.0, 0.0], top_k=2)
    assert results[0]["chunk_id"] == "a"
    assert set(results[0]) >= {
        "chunk_id", "text", "distance", "episode_guid",
        "episode_title", "episode_date", "episode_number",
        "start_time", "end_time",
    }


def test_load_current_rejects_corrupt_parquet(tmp_path):
    rows = [_row("a", "x", 251, [1.0, 0.0])]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="m", dims=2, git_sha="s", built_at="t",
    )
    # Corrupt the parquet so its sha256 no longer matches current.json
    (tmp_path / "corpus" / "v0001.parquet").write_bytes(b"corrupted")
    try:
        corpus.load_current(str(tmp_path))
        assert False, "expected a sha256 mismatch error"
    except ValueError as exc:
        assert "sha256" in str(exc).lower()
```

> NOTE for the executor: `hybrid_search` caches the loaded corpus per `(collection.name, count)`. Each test builds a fresh `pep_oracle`-named corpus with a possibly-equal count, so clear the cache at the top of any test that reuses the name. The two tests above use distinct counts (2 vs 1) and run in separate functions; if you add same-count cases, add `import pep_oracle.hybrid as hybrid; hybrid._CACHE.clear()` at the test top (mirrors `tests/test_hybrid.py`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_corpus.py -k "inmemory or load_current" -v`
Expected: FAIL (`corpus` has no `load_current` / `InMemoryCorpus`).

- [ ] **Step 3: Implement the loader half of `src/pep_oracle/corpus.py`**

Add these imports and code to `src/pep_oracle/corpus.py`. Add `from pep_oracle.config import CHROMA_COLLECTION` to the imports near the top, then append:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_corpus.py -v`
Expected: PASS (5 tests total in the file).

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/corpus.py tests/test_corpus.py
git commit -m "feat(aws): InMemoryCorpus loader, drop-in for hybrid_search retrieval"
```

---

## Task 6: Backfill module + CLI command

**Files:**
- Create: `src/pep_oracle/backfill.py`
- Create: `tests/fixtures/export_sample.json`
- Modify: `src/pep_oracle/cli.py`
- Test: `tests/test_backfill.py`

- [ ] **Step 1: Create the export fixture**

Create `tests/fixtures/export_sample.json` (shape matches `store.export_episodes`: `id`, `document`, `embedding` [old bge-large vectors to be discarded], `metadata`):

```json
[
  {
    "id": "ep251-chunk-0",
    "document": "the byrd rule constrains reconciliation in the senate",
    "embedding": [0.111, 0.222, 0.333],
    "metadata": {
      "episode_guid": "guid-251",
      "episode_title": "Ep 251",
      "episode_date": "2026-04-01",
      "episode_number": 251,
      "start_time": 12.5,
      "end_time": 45.0
    }
  },
  {
    "id": "ep253-chunk-0",
    "document": "section 122 tariffs and the trade deficit",
    "embedding": [0.444, 0.555, 0.666],
    "metadata": {
      "episode_guid": "guid-253",
      "episode_title": "Ep 253",
      "episode_date": "2026-05-01",
      "episode_number": 253,
      "start_time": 0.0,
      "end_time": 30.0
    }
  }
]
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_backfill.py`:

```python
import json

import pep_oracle.backfill as backfill
from pep_oracle import corpus


def _fixture_path():
    import pathlib

    return str(pathlib.Path(__file__).parent / "fixtures" / "export_sample.json")


def _fake_embed(texts):
    # Deterministic 2-d vectors that DIFFER from the fixture's old embeddings.
    return [[float(len(t)), 1.0] for t in texts]


def test_backfill_reembeds_and_publishes_v0001(tmp_path):
    manifest = backfill.backfill(
        export_path=_fixture_path(),
        dest=str(tmp_path),
        version="v0001",
        embed=_fake_embed,
        embed_model="amazon.titan-embed-text-v2:0",
        dims=2,
        git_sha="deadbee",
        built_at="2026-06-02T00:00:00+00:00",
    )

    assert manifest.chunk_count == 2
    assert manifest.episode_range == [251, 253]
    assert manifest.embed_model == "amazon.titan-embed-text-v2:0"

    c = corpus.load_current(str(tmp_path))
    got = c.get(include=["documents", "embeddings", "metadatas"])
    # ids + text + metadata preserved from the export
    assert got["ids"] == ["ep251-chunk-0", "ep253-chunk-0"]
    assert got["metadatas"][0]["episode_number"] == 251
    assert got["metadatas"][0]["start_time"] == 12.5
    # embeddings REPLACED by the new embedder (old bge-large vectors discarded)
    assert got["embeddings"][0] == [float(len("the byrd rule constrains reconciliation in the senate")), 1.0]
    assert got["embeddings"][0] != [0.111, 0.222, 0.333]


def test_backfill_embeds_each_document_once(tmp_path):
    seen = []

    def counting_embed(texts):
        seen.extend(texts)
        return [[1.0, 2.0] for _ in texts]

    backfill.backfill(
        export_path=_fixture_path(), dest=str(tmp_path), version="v0001",
        embed=counting_embed, embed_model="m", dims=2, git_sha="s",
        built_at="t",
    )
    assert seen == [
        "the byrd rule constrains reconciliation in the senate",
        "section 122 tariffs and the trade deficit",
    ]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_backfill.py -v`
Expected: FAIL (`No module named 'pep_oracle.backfill'`).

- [ ] **Step 4: Implement `src/pep_oracle/backfill.py`**

```python
"""One-time corpus backfill: re-embed the existing `pep-oracle export` JSON with
the Bedrock backend and publish v0001 of the corpus artifact.

Transcription/diarization are NOT re-run — the export already holds chunk text +
metadata; only the embedding vectors are recomputed (old bge-large vectors are
discarded). One Bedrock pass over ~10k short texts, a few cents.
"""

from __future__ import annotations

import json
import subprocess

from pep_oracle import config, corpus
from pep_oracle.embeddings import embed_texts


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — provenance only; never block a backfill
        return "unknown"


def backfill(
    *,
    export_path: str,
    dest: str,
    version: str = "v0001",
    embed=embed_texts,
    embed_model: str | None = None,
    dims: int | None = None,
    git_sha: str | None = None,
    built_at: str | None = None,
) -> corpus.Manifest:
    """Read export JSON, re-embed each chunk's text, publish <dest>/corpus/<version>.*."""
    with open(export_path) as f:
        items = json.load(f)

    texts = [it["document"] for it in items]
    vectors = embed(texts)
    rows = [
        {
            "chunk_id": it["id"],
            "text": it["document"],
            "embedding": vec,
            "metadata": it["metadata"],
        }
        for it, vec in zip(items, vectors)
    ]

    if built_at is None:
        from datetime import datetime, timezone

        built_at = datetime.now(timezone.utc).isoformat()

    return corpus.write_artifact(
        rows,
        dest=dest,
        version=version,
        embed_model=embed_model or config.EMBED_MODEL,
        dims=dims or config.EMBED_DIMS,
        git_sha=git_sha if git_sha is not None else globals()["git_sha"](),
        built_at=built_at,
    )
```

> NOTE: the `git_sha` parameter shadows the module-level `git_sha()`; the call `globals()["git_sha"]()` reaches the function when the param is left as `None`. If you prefer, rename the module function to `_current_git_sha()` and call that directly — adjust the test's `git_sha="deadbee"` keyword accordingly (it passes the value, so renaming the function doesn't affect the test).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_backfill.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Add the `backfill` CLI command**

In `src/pep_oracle/cli.py`, add a new command after the `import` command (after line 167, before the `backup` command). It guards that the Bedrock backend is active so the manifest's `embed_model` honestly matches the vectors:

```python
@cli.command(name="backfill")
@click.option("--export", "export_path", type=click.Path(exists=True), required=True,
              help="Path to a `pep-oracle export` JSON file to re-embed.")
@click.option("--out", "dest", default=None,
              help="Destination base (local dir or s3:// URI). Default: PEP_ORACLE_CORPUS_URI.")
@click.option("--version", default="v0001", help="Artifact version label (vNNNN).")
def backfill_cmd(export_path: str, dest: str | None, version: str) -> None:
    """Re-embed an exported corpus via Bedrock and publish a versioned artifact.

    Requires PEP_ORACLE_EMBED_BACKEND=bedrock so the published vectors (and the
    manifest's embed_model) are Titan, not the local bge-large model.
    """
    from pep_oracle import config
    from pep_oracle.backfill import backfill as run_backfill

    if config.EMBED_BACKEND != "bedrock":
        raise click.ClickException(
            "Set PEP_ORACLE_EMBED_BACKEND=bedrock before backfill so the artifact "
            "is Titan-embedded (the manifest records embed_model from config)."
        )
    dest = dest or config.CORPUS_URI
    manifest = run_backfill(export_path=export_path, dest=dest, version=version)
    click.echo(
        f"Published {version}: {manifest.chunk_count} chunks "
        f"(episodes {manifest.episode_range}) via {manifest.embed_model} "
        f"-> {dest}/corpus/{version}.parquet (sha256 {manifest.sha256[:12]}…)"
    )
```

- [ ] **Step 7: Verify the command wires up**

Run: `uv run pep-oracle backfill --help`
Expected: usage text listing `--export`, `--out`, `--version`.

Run (guard path, no AWS needed): `uv run pep-oracle backfill --export tests/fixtures/export_sample.json`
Expected: error `Set PEP_ORACLE_EMBED_BACKEND=bedrock before backfill ...` (because default backend is fastembed). This confirms the guard.

- [ ] **Step 8: Run the suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/pep_oracle/backfill.py tests/test_backfill.py tests/fixtures/export_sample.json src/pep_oracle/cli.py
git commit -m "feat(aws): backfill command re-embeds export JSON into a corpus artifact"
```

---

## Task 7: Eval harness over an artifact + `--corpus` CLI + no-regression gate

**Files:**
- Modify: `src/pep_oracle/eval_retrieval.py`
- Modify: `src/pep_oracle/cli.py`
- Test: `tests/test_eval_retrieval.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eval_retrieval.py`:

```python
import pep_oracle.hybrid as hybrid
from pep_oracle.corpus import InMemoryCorpus
from pep_oracle.eval_retrieval import evaluate_corpus, format_single


def _toy_corpus():
    hybrid._CACHE.clear()  # avoid cross-test corpus-cache bleed (same collection name)
    ids = ["a", "b"]
    docs = ["the byrd rule reconciliation senate", "weather and sports chit chat"]
    embeddings = [[1.0, 0.0], [0.0, 1.0]]
    metas = [
        {"episode_number": 251, "episode_date": "2026-04-01",
         "episode_guid": "g251", "episode_title": "Ep 251",
         "start_time": 0.0, "end_time": 10.0},
        {"episode_number": 252, "episode_date": "2026-04-08",
         "episode_guid": "g252", "episode_title": "Ep 252",
         "start_time": 0.0, "end_time": 10.0},
    ]
    return InMemoryCorpus(ids, docs, embeddings, metas)


def test_evaluate_corpus_scores_a_known_case():
    corpus = _toy_corpus()
    cases = [{"query": "byrd rule", "type": "specific_term", "phrase": "byrd rule"}]
    # Inject a deterministic embedder so the test needs no model/Bedrock.
    res = evaluate_corpus(corpus, embed=lambda texts: [[1.0, 0.0] for _ in texts], cases=cases)

    assert res["overall"]["n"] == 1
    assert res["overall"]["recall"][5] == 1.0  # 'a' contains "byrd rule"
    assert res["overall"]["mrr"] == 1.0        # and ranks first


def test_format_single_renders_overall_and_by_type():
    corpus = _toy_corpus()
    cases = [{"query": "byrd rule", "type": "specific_term", "phrase": "byrd rule"}]
    res = evaluate_corpus(corpus, embed=lambda texts: [[1.0, 0.0] for _ in texts], cases=cases)
    report = format_single("hybrid-titan", res)
    assert "OVERALL" in report
    assert "specific_term" in report
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_eval_retrieval.py -k "evaluate_corpus or format_single" -v`
Expected: FAIL (`evaluate_corpus` / `format_single` do not exist).

- [ ] **Step 3: Extend `src/pep_oracle/eval_retrieval.py`**

Replace the existing `_hybrid_retriever` function (lines 120-126) with a version that accepts an injectable embedder:

```python
def _hybrid_retriever(collection, embed=None):
    from pep_oracle.hybrid import hybrid_search

    if embed is None:
        from pep_oracle.embeddings import embed_texts

        embed = embed_texts

    def fn(query, top_k):
        return hybrid_search(collection, query, embed([query])[0], top_k=top_k)

    return fn
```

Then add two new functions (after `run_comparison`, near line 141):

```python
def evaluate_corpus(corpus, embed=None, cases=CASES, ks=(5, 10)) -> dict:
    """Run the hybrid retrieval eval over an InMemoryCorpus (a parquet artifact),
    so retrieval quality can be measured for a Bedrock-embedded corpus and
    compared against the bge-large ChromaDB baseline from run_comparison().

    NOTE: when measuring a Titan artifact for real, the query embedder MUST also
    be Titan (same vector space) — i.e. run with PEP_ORACLE_EMBED_BACKEND=bedrock,
    or pass `embed` explicitly. Mismatched query/corpus embedders are meaningless.
    """
    got = corpus.get(include=["documents", "metadatas"])
    return evaluate(
        _hybrid_retriever(corpus, embed), got["documents"], got["metadatas"],
        cases=cases, ks=ks,
    )


def format_single(name: str, res: dict, ks=(5, 10)) -> str:
    """Render one evaluate() result (format_report requires the semantic+hybrid
    pair; this handles a single retriever, e.g. a corpus-artifact eval)."""
    o = res["overall"]
    lines = ["=== OVERALL ===",
             "retriever  " + "  ".join(f"recall@{k}" for k in ks) + "   MRR"]
    cells = "   ".join(f"{o['recall'][k]:.2f}    " for k in ks)
    lines.append(f"{name:14}  {cells}  {o['mrr']:.2f}  (n={o['n']})")
    lines.append("\n=== recall@%d by query type ===" % ks[-1])
    for t, agg in res["by_type"].items():
        lines.append(f"  {t:18} {agg['recall'][ks[-1]]:.2f}  (n={agg['n']})")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_eval_retrieval.py -v`
Expected: PASS (existing tests + the two new ones).

- [ ] **Step 5: Add `--corpus` to the `eval-retrieval` CLI**

Replace the existing `eval_retrieval_cmd` (`src/pep_oracle/cli.py` lines 79-85) with:

```python
@cli.command(name="eval-retrieval")
@click.option("--corpus", "corpus_uri", default=None,
              help="Eval hybrid retrieval over a corpus artifact (local dir or s3:// base) "
                   "instead of the live ChromaDB. Use PEP_ORACLE_EMBED_BACKEND=bedrock so the "
                   "query embedder matches a Titan artifact.")
def eval_retrieval_cmd(corpus_uri: str | None) -> None:
    """Score retrieval quality (recall@k, MRR) on a labeled query set.

    Default: compare semantic-only vs hybrid over the live ChromaDB (bge-large).
    With --corpus: score hybrid over the parquet artifact (Bedrock-embedded), to
    confirm no regression vs the bge-large baseline before promoting the artifact.
    """
    from pep_oracle.eval_retrieval import (
        evaluate_corpus, format_report, format_single, run_comparison,
    )

    if corpus_uri:
        from pep_oracle.corpus import load_current

        corpus = load_current(corpus_uri)
        click.echo(format_single(f"hybrid({corpus.version})", evaluate_corpus(corpus)))
    else:
        click.echo(format_report(run_comparison()))
```

- [ ] **Step 6: Verify the command wires up**

Run: `uv run pep-oracle eval-retrieval --help`
Expected: usage text including `--corpus`.

- [ ] **Step 7: Run the suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/pep_oracle/eval_retrieval.py src/pep_oracle/cli.py tests/test_eval_retrieval.py
git commit -m "feat(aws): eval retrieval over a corpus artifact + --corpus CLI for the no-regression gate"
```

---

## Task 8: Migration runbook + CLAUDE.md note

**Files:**
- Create: `docs/aws/phase1-backfill-runbook.md`
- Modify: `CLAUDE.md`

This task documents (and is where the operator performs) the actual one-time migration, which needs AWS credentials and is therefore a manual, non-automated step.

- [ ] **Step 1: Write the runbook**

Create `docs/aws/phase1-backfill-runbook.md`:

````markdown
# Phase 1 backfill runbook — Bedrock re-embed of the existing corpus

One-time migration: re-embed the 95 ingested episodes with Bedrock Titan v2 and
publish `v0001` of the corpus artifact. No Modal/GPU; transcription/diarization
are not re-run. Cost: one Bedrock pass over ~10k short texts (a few cents).

## Prerequisites
- AWS credentials with `bedrock:InvokeModel` on `amazon.titan-embed-text-v2:0` in
  `ap-southeast-2`, and (if publishing to S3) `s3:PutObject` on the target bucket.
  Bedrock model access for Titan Text Embeddings V2 must be enabled in the account
  (Bedrock console → Model access).
- `uv pip install -e ".[aws]"`

## Steps

1. Export the current corpus from the box that holds the ingested ChromaDB:
   ```bash
   uv run pep-oracle export /tmp/corpus-export.json
   ```

2. Re-embed + publish (local artifact first, to inspect before S3):
   ```bash
   export PEP_ORACLE_EMBED_BACKEND=bedrock
   export PEP_ORACLE_BEDROCK_REGION=ap-southeast-2
   export AWS_PROFILE=...   # or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
   uv run pep-oracle backfill --export /tmp/corpus-export.json --out ~/.pep-oracle --version v0001
   ```
   Reports chunk count, episode range, model, and sha256. Artifact lands at
   `~/.pep-oracle/corpus/v0001.parquet` (the `--out` base + the `/corpus` prefix).

3. Validate retrieval quality (no-regression gate). Compare the two reports —
   the Titan hybrid recall@10 / MRR must hold vs the bge-large baseline:
   ```bash
   # bge-large baseline (default backend), over the live ChromaDB:
   PEP_ORACLE_EMBED_BACKEND=fastembed uv run pep-oracle eval-retrieval
   # Titan artifact (query embedder must also be Titan):
   PEP_ORACLE_EMBED_BACKEND=bedrock   uv run pep-oracle eval-retrieval --corpus ~/.pep-oracle
   ```
   Gate: artifact `recall@10` ≥ baseline `recall@10` (and MRR within ~0.02). If
   Titan regresses, re-run the backfill with Cohere (`PEP_ORACLE_EMBED_MODEL=cohere.embed-english-v3`,
   `PEP_ORACLE_EMBED_DIMS=1024`) and re-compare; pin the winner.

4. Publish to S3 (once a bucket exists — Phase 2/3 CDK creates the prod bucket;
   for now any private bucket in `ap-southeast-2` works for validation):
   ```bash
   uv run pep-oracle backfill --export /tmp/corpus-export.json --out s3://<bucket> --version v0001
   ```

5. Inspect the artifact (optional):
   ```bash
   uv run python -c "import pyarrow.parquet as pq; t=pq.read_table('$HOME/.pep-oracle/corpus/v0001.parquet'); print(t.schema); print(t.num_rows)"
   ```
````

- [ ] **Step 2: Add a CLAUDE.md note**

In `CLAUDE.md`, under **Key design decisions**, replace the existing **Local embeddings** bullet's first sentence to note the pluggable backend, and add a corpus-artifact bullet. Concretely, add this bullet after the **Local embeddings** bullet:

```markdown
- **Embedding backend is pluggable** (`embeddings.py`): `PEP_ORACLE_EMBED_BACKEND` selects `fastembed` (local bge-large, default) or `bedrock` (AWS Titan `amazon.titan-embed-text-v2:0` in `ap-southeast-2`, 1024-dim). Query and corpus vectors must share one backend+model. **Corpus artifact** (`corpus.py`): a versioned immutable parquet (`<CORPUS_URI>/corpus/vNNNN.parquet` + `.manifest.json` + mutable `current.json`) re-loadable into an `InMemoryCorpus` that is a drop-in for `hybrid_search`/`get_ingestion_stats` (no ChromaDB on that path). Build it from a `pep-oracle export` JSON with `pep-oracle backfill` (requires `EMBED_BACKEND=bedrock`); validate with `pep-oracle eval-retrieval --corpus <base>`. See `docs/aws/phase1-backfill-runbook.md`. Part of the AWS migration (`docs/superpowers/specs/2026-06-02-aws-mcp-migration-design.md`).
```

> NOTE: `CLAUDE.md` is currently within its line budget; keep this addition tight. The repo's PreToolUse commit hook BLOCKS `git commit` until `/claude-md-improver` has been run and `.claude/.md-reviewed` touched whenever CLAUDE.md changed — so run `/claude-md-improver` (or the documented equivalent) and stage CLAUDE.md before committing this task.

- [ ] **Step 3: Run the full suite a final time**

Run: `uv run pytest -q`
Expected: PASS (all Phase 1 tests + the pre-existing suite).

- [ ] **Step 4: Commit**

```bash
git add docs/aws/phase1-backfill-runbook.md CLAUDE.md .claude/.md-reviewed
git commit -m "docs(aws): Phase 1 backfill runbook + CLAUDE.md embedding/corpus notes"
```

---

## Self-Review (completed by plan author)

**Spec coverage (Phase 1 slice of the AWS migration spec):**
- Drop bge-large → Bedrock embeddings (Titan v2, validate vs Cohere via eval harness) → Task 2 (backend) + Task 7/Task 8 (eval gate + Cohere fallback documented). ✓
- Versioned flat S3 corpus artifact (parquet + manifest + `current.json`, write-then-flip atomic) → Task 4 (writer) + Task 3 (s3 dispatch). ✓
- Loader into memory, drop-in for retrieval (no ChromaDB on serve path) → Task 5 (`InMemoryCorpus`). ✓
- One-time backfill from the export JSON, re-embed only (no Modal/GPU) → Task 6. ✓
- Manifest carries schema_ver, embed_model, dims, episode_range, chunk_count, ingest_git_sha, built_at, sha256 → Task 4 `Manifest`. ✓
- Local/CI parity via `PEP_ORACLE_CORPUS_URI` selecting source → Task 1 config + Task 3/5. ✓
- Region `ap-southeast-2` baked into `BEDROCK_REGION` default → Task 1. ✓
- **Deferred to later phases (correctly out of this plan):** Lambda/Mangum serving, `GET /version` endpoint, warm-container `current.json` TTL refresh + atomic swap, OAuth→DynamoDB, Cognito gate, CloudFront/OAC, CDK, CI/CD, KMS signing. These consume the artifact + loader this phase delivers.

**Placeholder scan:** No TBD/"add error handling"/"write tests for the above" — every code and test step contains complete content. Throttling retry, sha256 verification, and the bedrock-backend guard are all concretely implemented.

**Type consistency:** `embed_texts(list[str]) -> list[list[float]]` preserved across Tasks 2/6/7. `Manifest` fields used in Task 4 match the assertions in Tasks 4/6 (`chunk_count`, `episode_range`, `embed_model`, `dims`, `sha256`). `InMemoryCorpus.get(include=...)` returns the same `{ids, documents, embeddings, metadatas}` keys that `hybrid._load_corpus` and `store.get_ingestion_stats` read (verified against `hybrid.py:41` and `store.py:144`). `write_artifact(...)` keyword args in Task 4 match the call in `backfill.backfill` (Task 6) and the tests. `load_current(base)` (Task 5) is called with the same `--out`/`CORPUS_URI` base used by `write_artifact` (Tasks 6/7/8). `evaluate_corpus(corpus, embed=None, cases, ks)` and `format_single(name, res, ks)` signatures (Task 7) match their test calls.

---

## Execution Handoff

Phase 1 plan saved to `docs/superpowers/plans/2026-06-02-aws-migration-phase1-embedding-corpus.md`. Per your standing preference, I'll execute it with **subagent-driven development** (fresh subagent per task, two-stage review between tasks) unless you say otherwise. The one task that needs your AWS credentials (Task 8's actual backfill + eval gate) I'll prepare and hand to you with exact commands rather than run blind.
