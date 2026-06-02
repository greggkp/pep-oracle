# AWS Migration Phase 2a — Serving Core (corpus-from-artifact + Mangum + /version) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the MCP serving path retrieve from the Phase 1 corpus artifact (no ChromaDB) behind a config gate, with bounded-staleness refresh, a Lambda entrypoint (Mangum), and a `GET /version` endpoint — the app-code half of "serving on Lambda," fully runnable and testable locally.

**Architecture:** A config flag `PEP_ORACLE_SERVE_FROM_ARTIFACT` selects the retrieval source: when off (default) the MCP tool keeps using the live ChromaDB collection (the OptiPlex is unchanged — nothing rebuilds the artifact on ingest until Phase 3); when on (the Lambda) it serves an `InMemoryCorpus` loaded from `PEP_ORACLE_CORPUS_URI` via a process-cached loader that re-checks `current.json` on a TTL and atomically swaps to a new version. `server.py` keeps its existing FastAPI `app`; we add a Mangum `handler` for Lambda and a `/version` endpoint. This phase also fixes the three Phase 1 carry-forwards (hybrid cache keyed on `(name, version)`; artifact dims validated against the manifest; query embedder validated against the artifact's `embed_model`).

**Tech Stack:** FastAPI + Mangum (ASGI→Lambda), the Phase 1 `corpus.py`/`_storage.py`/`embeddings.py` modules, boto3/pyarrow, pytest. Region `ap-southeast-2`.

**Scope boundary:** This phase is **app code only**, runnable with `uvicorn` and pytest. It does **not** provision or deploy anything (no CDK, CloudFront, real Cognito, real DynamoDB/S3 bucket) and does **not** touch OAuth state, JWT signing backends, or the Cognito gate — those are **Phase 2b** (auth migration) and **Phase 2c** (CDK + deploy). OAuth here remains the existing SQLite/HS256 path, unchanged. The `/ask` web path stays on ChromaDB regardless of the flag (out of migration scope).

---

## File Structure

**Modified:**
- `pyproject.toml` — add `mangum` to the `aws` extra and the `dev` group.
- `src/pep_oracle/config.py` — `SERVE_FROM_ARTIFACT`, `CORPUS_REFRESH_TTL_SECONDS`, `GIT_SHA`.
- `src/pep_oracle/hybrid.py` — key the in-process corpus cache on `(name, version)` (carry-forward #1).
- `src/pep_oracle/corpus.py` — `load_manifest()`, serving validation (`_validate_serving`), and the TTL-cached `current_corpus()` with atomic swap.
- `src/pep_oracle/mcp_server.py` — `get_serving_corpus()` source seam; `search_pep` uses it.
- `src/pep_oracle/server.py` — Mangum `handler`; `GET /version`.
- `tests/test_hybrid.py`, `tests/test_corpus.py`, `tests/test_mcp_server.py`, `tests/test_server.py` — new tests.
- `CLAUDE.md` — serving-source flag + `/version` note.

**No new source files** — everything extends Phase 1 modules. The serving-cache state lives in `corpus.py` next to the loader it caches.

---

## Task 1: Fix hybrid corpus cache to key on `(name, version)` — carry-forward #1

**Files:**
- Modify: `src/pep_oracle/hybrid.py:35-52` (`_load_corpus`)
- Test: `tests/test_hybrid.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_hybrid.py`:

```python
def test_cache_keys_on_version_not_just_name():
    """Two InMemoryCorpus instances share the name 'pep_oracle' and the same chunk
    count; only `.version` differs. The cache must NOT serve the first one's data
    for the second (the bug that would defeat the serving-path atomic swap)."""
    from pep_oracle.corpus import InMemoryCorpus

    hybrid._CACHE.clear()

    def _meta(ep):
        return {"episode_number": ep, "episode_date": f"2026-01-0{ep}",
                "episode_guid": f"g{ep}", "episode_title": f"Ep {ep}",
                "start_time": 0.0, "end_time": 1.0}

    a = InMemoryCorpus(["a"], ["byrd rule reconciliation"], [[1.0, 0.0]], [_meta(1)], version="v0001")
    b = InMemoryCorpus(["b"], ["tariffs section 122"], [[1.0, 0.0]], [_meta(2)], version="v0002")

    ra = hybrid_search(a, "byrd rule", [1.0, 0.0], top_k=1)
    rb = hybrid_search(b, "tariffs", [1.0, 0.0], top_k=1)

    assert ra[0]["chunk_id"] == "a"
    assert rb[0]["chunk_id"] == "b"  # not stale 'a' from a name-only cache key
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_hybrid.py::test_cache_keys_on_version_not_just_name -v`
Expected: FAIL — `rb[0]["chunk_id"]` is `"a"` (the cache returns corpus `a` because the key is `name` only and `count` matches).

- [ ] **Step 3: Key the cache on `(name, version)`** — replace `_load_corpus` in `src/pep_oracle/hybrid.py`:

```python
def _load_corpus(collection) -> dict:
    name = collection.name
    version = getattr(collection, "version", None)  # InMemoryCorpus carries a version; Chroma doesn't
    count = collection.count()
    key = (name, version)
    cached = _CACHE.get(key)
    if cached is not None and cached["count"] == count:
        return cached
    got = collection.get(include=["documents", "embeddings", "metadatas"])
    docs = got["documents"]
    corpus = {
        "count": count,
        "ids": got["ids"],
        "docs": docs,
        "embeddings": got["embeddings"],
        "metas": got["metadatas"],
        "bm25": BM25([normalize_numbers(d or "") for d in docs]),
    }
    _CACHE[key] = corpus
    return corpus
```

Also update the cache-comment above `_CACHE` (line ~30) to reflect the key:

```python
# Per-corpus cache keyed by (name, version) + invalidated on chunk-count change.
# ChromaDB collections have no `.version` (-> None), so the live /ask+MCP-over-Chroma
# behavior is unchanged; InMemoryCorpus carries `.version` so a new artifact swap
# gets a fresh BM25 index instead of colliding with the previous one.
_CACHE: dict = {}  # (name, version) -> {count, ids, docs, embeddings, metas, bm25}
```

- [ ] **Step 4: Run the new test + the whole hybrid suite**

Run: `uv run pytest tests/test_hybrid.py -v`
Expected: PASS (the new test + all pre-existing hybrid tests; the latter use distinct collection names so keying by `(name, None)` is still distinct).

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/hybrid.py tests/test_hybrid.py
git commit -m "fix(serving): key hybrid corpus cache on (name, version) so artifact swaps take effect"
```

---

## Task 2: Serving config flags + mangum dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/pep_oracle/config.py`

- [ ] **Step 1: Add `mangum` to the `aws` extra and dev group in `pyproject.toml`**

```toml
[project.optional-dependencies]
server = ["fastapi", "uvicorn[standard]", "mcp>=1.0", "pyjwt[crypto]>=2.8"]
aws = ["boto3>=1.34", "pyarrow>=15", "mangum>=0.17"]
```

```toml
[dependency-groups]
dev = [
    "pytest-asyncio>=1.3.0",
    "boto3>=1.34",
    "pyarrow>=15",
    "mangum>=0.17",
]
```

- [ ] **Step 2: Add serving config knobs in `src/pep_oracle/config.py`** — after the Phase 1 `CORPUS_URI` block, add:

```python
# --- Serving source (Phase 2a) ---
# When "1", the MCP tool retrieves from the corpus artifact at CORPUS_URI via an
# in-memory InMemoryCorpus (the Lambda path); otherwise it uses the live ChromaDB
# collection (the OptiPlex default — nothing rebuilds the artifact on ingest until
# Phase 3). Serving from the artifact REQUIRES EMBED_BACKEND=bedrock with a model
# matching the artifact's manifest (validated at load).
SERVE_FROM_ARTIFACT = os.getenv("PEP_ORACLE_SERVE_FROM_ARTIFACT", "0") == "1"
# How often a warm process re-checks current.json for a new corpus version (a cheap
# small-object GET). New episodes reach a warm container within this window.
CORPUS_REFRESH_TTL_SECONDS = int(os.getenv("PEP_ORACLE_CORPUS_REFRESH_TTL_SECONDS", "300"))
# Baked into the image at build time (Phase 2c); reported by GET /version.
GIT_SHA = os.getenv("PEP_ORACLE_GIT_SHA", "")
```

- [ ] **Step 3: Install + smoke-check**

Run:
```bash
uv pip install -e ".[server,aws]"
uv run python -c "import mangum; from pep_oracle import config; print(config.SERVE_FROM_ARTIFACT, config.CORPUS_REFRESH_TTL_SECONDS, repr(config.GIT_SHA))"
```
Expected: `False 300 ''`

- [ ] **Step 4: Suite still green**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/pep_oracle/config.py
git commit -m "feat(serving): mangum dep + SERVE_FROM_ARTIFACT/TTL/GIT_SHA config knobs"
```

---

## Task 3: Manifest loader + serving validation — carry-forwards #2 and #3

**Files:**
- Modify: `src/pep_oracle/corpus.py`
- Test: `tests/test_corpus.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_corpus.py`:

```python
from pep_oracle import config as _config


def test_load_manifest_returns_version_and_manifest(tmp_path):
    rows = [_row("a", "x", 251, [1.0, 0.0]), _row("b", "y", 253, [0.0, 1.0])]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0007",
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s",
        built_at="2026-06-02T00:00:00+00:00",
    )
    version, manifest = corpus.load_manifest(str(tmp_path))
    assert version == "v0007"
    assert manifest.embed_model == "amazon.titan-embed-text-v2:0"
    assert manifest.dims == 2
    assert manifest.episode_range == [251, 253]


def test_validate_serving_passes_when_dims_and_model_match(tmp_path, monkeypatch):
    rows = [_row("a", "x", 251, [1.0, 0.0])]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s", built_at="t",
    )
    monkeypatch.setattr(_config, "EMBED_BACKEND", "bedrock")
    monkeypatch.setattr(_config, "EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    c = corpus.load_current(str(tmp_path))
    corpus._validate_serving(c, str(tmp_path))  # no raise


def test_validate_serving_raises_on_embed_model_mismatch(tmp_path, monkeypatch):
    rows = [_row("a", "x", 251, [1.0, 0.0])]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s", built_at="t",
    )
    monkeypatch.setattr(_config, "EMBED_BACKEND", "fastembed")  # bge-large queries vs Titan corpus
    monkeypatch.setattr(_config, "EMBED_MODEL", "BAAI/bge-large-en-v1.5")
    c = corpus.load_current(str(tmp_path))
    try:
        corpus._validate_serving(c, str(tmp_path))
        assert False, "expected an embedder-mismatch error"
    except ValueError as exc:
        assert "embed" in str(exc).lower()


def test_validate_serving_raises_on_dims_mismatch(tmp_path, monkeypatch):
    rows = [_row("a", "x", 251, [1.0, 0.0])]  # 2-d vectors
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="amazon.titan-embed-text-v2:0", dims=99, git_sha="s", built_at="t",  # manifest lies: 99 != 2
    )
    monkeypatch.setattr(_config, "EMBED_BACKEND", "bedrock")
    monkeypatch.setattr(_config, "EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    c = corpus.load_current(str(tmp_path))
    try:
        corpus._validate_serving(c, str(tmp_path))
        assert False, "expected a dims-mismatch error"
    except ValueError as exc:
        assert "dim" in str(exc).lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_corpus.py -k "load_manifest or validate_serving" -v`
Expected: FAIL (`corpus` has no `load_manifest` / `_validate_serving`).

- [ ] **Step 3: Implement** — add to `src/pep_oracle/corpus.py` (it already imports `json`, `hashlib`, `storage`, and defines `Manifest`/`load_current`). Add `from pep_oracle import config` to the imports near the top, then append:

```python
def load_manifest(base: str) -> tuple[str, Manifest]:
    """Read <base>/corpus/current.json + the version's manifest. Returns (version, Manifest)."""
    prefix = str(base).rstrip("/") + "/corpus"
    cur = json.loads(storage.get_text(f"{prefix}/current.json"))
    version = cur["version"]
    m = json.loads(storage.get_text(f"{prefix}/{version}.manifest.json"))
    return version, Manifest(**m)


def _validate_serving(corpus: "InMemoryCorpus", base: str) -> None:
    """Guard the serving path against a corpus/embedder mismatch:
      1. The manifest dims must match the loaded vectors' width.
      2. The active query embedder (config) must match the artifact's embed_model,
         else queries would be embedded in a different vector space than the corpus.
    Raises ValueError on either mismatch."""
    _version, manifest = load_manifest(base)
    if corpus.embeddings:
        actual_dims = len(corpus.embeddings[0])
        if actual_dims != manifest.dims:
            raise ValueError(
                f"corpus dims mismatch: manifest={manifest.dims} but vectors are {actual_dims}-d"
            )
    if config.EMBED_BACKEND != "bedrock" or config.EMBED_MODEL != manifest.embed_model:
        raise ValueError(
            f"query embedder mismatch: serving a {manifest.embed_model} corpus requires "
            f"EMBED_BACKEND=bedrock + EMBED_MODEL={manifest.embed_model}, but config has "
            f"backend={config.EMBED_BACKEND!r} model={config.EMBED_MODEL!r}"
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_corpus.py -v`
Expected: PASS (Phase 1 tests + the 4 new ones).

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/corpus.py tests/test_corpus.py
git commit -m "feat(serving): load_manifest + _validate_serving (dims + embedder match)"
```

---

## Task 4: TTL-cached serving corpus with atomic swap

**Files:**
- Modify: `src/pep_oracle/corpus.py`
- Test: `tests/test_corpus.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_corpus.py`:

```python
import pep_oracle.hybrid as _hybrid


def _publish(tmp_path, version, ep, text):
    corpus.write_artifact(
        [_row("c", text, ep, [1.0, 0.0])],
        dest=str(tmp_path), version=version,
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s", built_at="t",
    )


def _serving_config(monkeypatch):
    monkeypatch.setattr(_config, "EMBED_BACKEND", "bedrock")
    monkeypatch.setattr(_config, "EMBED_MODEL", "amazon.titan-embed-text-v2:0")


def test_current_corpus_caches_within_ttl(tmp_path, monkeypatch):
    _serving_config(monkeypatch)
    corpus.reset_serving_cache()
    _publish(tmp_path, "v0001", 251, "first")

    clock = {"t": 1000.0}
    c1 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c1.version == "v0001"

    # Publish a new version, but stay within the TTL window -> cached v0001 returned,
    # current.json is NOT even re-read.
    _publish(tmp_path, "v0002", 252, "second")
    clock["t"] = 1000.0 + 299
    c2 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c2 is c1
    assert c2.version == "v0001"


def test_current_corpus_swaps_after_ttl_when_version_changes(tmp_path, monkeypatch):
    _serving_config(monkeypatch)
    corpus.reset_serving_cache()
    _hybrid._CACHE.clear()
    _publish(tmp_path, "v0001", 251, "byrd rule")

    clock = {"t": 0.0}
    c1 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c1.version == "v0001"

    _publish(tmp_path, "v0002", 252, "tariffs")
    clock["t"] = 400.0  # past the TTL
    c2 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c2.version == "v0002"
    assert c2 is not c1

    # And retrieval reflects the swap (relies on the Task 1 cache-key fix):
    res = _hybrid.hybrid_search(c2, "tariffs", [1.0, 0.0], top_k=1)
    assert res[0]["episode_number"] == 252


def test_current_corpus_keeps_cache_after_ttl_if_version_unchanged(tmp_path, monkeypatch):
    _serving_config(monkeypatch)
    corpus.reset_serving_cache()
    _publish(tmp_path, "v0001", 251, "first")

    clock = {"t": 0.0}
    c1 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    clock["t"] = 400.0  # past TTL, but no new version published
    c2 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c2 is c1  # same object — version unchanged, no reload
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_corpus.py -k current_corpus -v`
Expected: FAIL (`corpus` has no `current_corpus` / `reset_serving_cache`).

- [ ] **Step 3: Implement** — add to `src/pep_oracle/corpus.py`. Add `import threading` and `import time` to the top imports, then append:

```python
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
        _SERVING["checked_at"] = t  # unchanged — extend the window, no reload
        return cached

    fresh = load_current(base)
    _validate_serving(fresh, base)
    with _SERVING_LOCK:
        _SERVING["corpus"] = fresh
        _SERVING["version"] = fresh.version
        _SERVING["checked_at"] = t
    return fresh
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_corpus.py -v`
Expected: PASS (all corpus tests, including the 4 new current_corpus tests).

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/corpus.py tests/test_corpus.py
git commit -m "feat(serving): TTL-cached current_corpus with atomic version swap"
```

---

## Task 5: MCP tool serves from the source seam

**Files:**
- Modify: `src/pep_oracle/mcp_server.py`
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_mcp_server.py`:

```python
def test_get_serving_corpus_uses_artifact_when_flagged(tmp_path, monkeypatch):
    import pep_oracle.config as config
    import pep_oracle.corpus as corpus
    import pep_oracle.hybrid as hybrid
    import pep_oracle.mcp_server as mcp_server

    corpus.write_artifact(
        [
            {"chunk_id": "z1", "text": "the byrd rule reconciliation senate",
             "embedding": [1.0, 0.0],
             "metadata": {"episode_number": 251, "episode_date": "2026-04-01",
                          "episode_guid": "g", "episode_title": "Ep 251",
                          "start_time": 0.0, "end_time": 10.0}},
        ],
        dest=str(tmp_path), version="v0001",
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s", built_at="t",
    )
    monkeypatch.setattr(config, "SERVE_FROM_ARTIFACT", True)
    monkeypatch.setattr(config, "CORPUS_URI", str(tmp_path))
    monkeypatch.setattr(config, "EMBED_BACKEND", "bedrock")
    monkeypatch.setattr(config, "EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    corpus.reset_serving_cache()
    hybrid._CACHE.clear()

    c = mcp_server.get_serving_corpus()
    assert c.__class__.__name__ == "InMemoryCorpus"
    assert c.version == "v0001"


def test_get_serving_corpus_uses_chroma_by_default(monkeypatch):
    import pep_oracle.config as config
    import pep_oracle.mcp_server as mcp_server

    monkeypatch.setattr(config, "SERVE_FROM_ARTIFACT", False)
    sentinel = object()
    monkeypatch.setattr(mcp_server, "get_fresh_collection", lambda: sentinel)
    assert mcp_server.get_serving_corpus() is sentinel
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -k get_serving_corpus -v`
Expected: FAIL (`mcp_server` has no `get_serving_corpus`).

- [ ] **Step 3: Implement** — in `src/pep_oracle/mcp_server.py`, add the import and seam, and switch `search_pep` to use it.

Add near the existing imports:

```python
from pep_oracle import config, corpus as corpus_mod
```

Add this function above `search_pep`:

```python
def get_serving_corpus():
    """Retrieval source seam: the corpus artifact (InMemoryCorpus) when
    PEP_ORACLE_SERVE_FROM_ARTIFACT=1 (the Lambda path), else the live ChromaDB
    collection (the OptiPlex default). Both satisfy hybrid_search +
    get_ingestion_stats; the artifact path validates dims + embedder at load."""
    if config.SERVE_FROM_ARTIFACT:
        return corpus_mod.current_corpus(
            config.CORPUS_URI, ttl_seconds=config.CORPUS_REFRESH_TTL_SECONDS
        )
    return get_fresh_collection()
```

In `search_pep`, replace the line:

```python
    collection = get_fresh_collection()
```

with:

```python
    collection = get_serving_corpus()
```

(Leave the rest of `search_pep` — `embed_texts`, `hybrid_search`, `temporal.select_for_intent`, `get_ingestion_stats` — unchanged; they already work over either source.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS (the 2 new tests + all pre-existing mcp_server tests, which run with `SERVE_FROM_ARTIFACT` defaulting False → ChromaDB path unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(serving): MCP tool retrieves via get_serving_corpus (artifact|chroma seam)"
```

---

## Task 6: Mangum Lambda handler

**Files:**
- Modify: `src/pep_oracle/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_server.py`:

```python
def test_lambda_handler_is_constructed():
    """server.handler is a Mangum ASGI adapter wrapping the FastAPI app, so the
    same app runs under uvicorn locally and Lambda in prod."""
    from pep_oracle import server

    assert server.handler is not None
    assert server.handler.__class__.__name__ == "Mangum"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_server.py::test_lambda_handler_is_constructed -v`
Expected: FAIL (`server` has no `handler`).

- [ ] **Step 3: Implement** — at the end of `src/pep_oracle/server.py`, after `mount_mcp_if_configured(app)` and before `def main()`, add:

```python
def _make_lambda_handler():
    """Wrap the ASGI app with Mangum for AWS Lambda. Returns None if mangum isn't
    installed (e.g. a base local install), so importing server stays cheap."""
    try:
        from mangum import Mangum
    except ImportError:
        return None
    return Mangum(app)


handler = _make_lambda_handler()
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_server.py::test_lambda_handler_is_constructed -v`
Expected: PASS (mangum is installed via the `aws`/dev deps from Task 2).

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/server.py tests/test_server.py
git commit -m "feat(serving): Mangum Lambda handler wrapping the FastAPI app"
```

---

## Task 7: `GET /version` endpoint

**Files:**
- Modify: `src/pep_oracle/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_server.py`. (These use FastAPI's `TestClient`, already imported at the top of `test_server.py`.)

```python
def test_version_reports_code_only_by_default(monkeypatch):
    from fastapi.testclient import TestClient
    from pep_oracle import config, server

    monkeypatch.setattr(config, "SERVE_FROM_ARTIFACT", False)
    monkeypatch.setattr(config, "GIT_SHA", "abc1234")
    with TestClient(server.app) as client:
        r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert body["code_git_sha"] == "abc1234"
    assert "code_semver" in body
    assert "corpus_version" not in body  # artifact serving off


def test_version_reports_corpus_when_serving_from_artifact(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from pep_oracle import config, corpus, server

    corpus.write_artifact(
        [{"chunk_id": "a", "text": "x", "embedding": [1.0, 0.0],
          "metadata": {"episode_number": 251, "episode_date": "2026-04-01",
                       "episode_guid": "g", "episode_title": "t",
                       "start_time": 0.0, "end_time": 1.0}}],
        dest=str(tmp_path), version="v0042",
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s",
        built_at="2026-06-01T06:14:00+00:00",
    )
    monkeypatch.setattr(config, "SERVE_FROM_ARTIFACT", True)
    monkeypatch.setattr(config, "CORPUS_URI", str(tmp_path))
    with TestClient(server.app) as client:
        r = client.get("/version")
    body = r.json()
    assert body["corpus_version"] == "v0042"
    assert body["corpus_episode_range"] == [251, 251]
    assert body["embed_model"] == "amazon.titan-embed-text-v2:0"
    assert body["corpus_built_at"] == "2026-06-01T06:14:00+00:00"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_server.py -k version -v`
Expected: FAIL — `/version` returns 404 (route not defined).

- [ ] **Step 3: Implement** — in `src/pep_oracle/server.py`:

Add to the imports at the top (the file already imports `os`, `subprocess` is new):

```python
import subprocess
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from pep_oracle import config as _config, corpus as _corpus
```

Add the endpoint next to `/health` (after the `health()` function, ~line 246):

```python
def _code_version() -> tuple[str, str]:
    sha = _config.GIT_SHA.strip()
    if not sha:
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except Exception:  # noqa: BLE001 — version info only; never fail the endpoint
            sha = "unknown"
    try:
        semver = _pkg_version("pep-oracle")
    except PackageNotFoundError:
        semver = "0.0.0"
    return semver, sha


@app.get("/version")
async def api_version():
    semver, sha = _code_version()
    out = {"code_semver": semver, "code_git_sha": sha}
    if _config.SERVE_FROM_ARTIFACT:
        try:
            version, manifest = _corpus.load_manifest(_config.CORPUS_URI)
            out.update(
                corpus_version=version,
                corpus_episode_range=manifest.episode_range,
                corpus_built_at=manifest.built_at,
                embed_model=manifest.embed_model,
                corpus_dims=manifest.dims,
            )
        except Exception as exc:  # noqa: BLE001 — surface, don't 500 the version probe
            out["corpus_error"] = str(exc)
    return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_server.py -k version -v`
Expected: PASS (both new tests).

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pep_oracle/server.py tests/test_server.py
git commit -m "feat(serving): GET /version reporting code + corpus versions"
```

---

## Task 8: Docs + CLAUDE.md + local smoke

**Files:**
- Create: `docs/aws/phase2a-serving-local-smoke.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write the local smoke doc** — create `docs/aws/phase2a-serving-local-smoke.md`:

````markdown
# Phase 2a — serving the MCP tool from the corpus artifact (local smoke)

Phase 2a lets the MCP tool retrieve from the Phase 1 corpus artifact instead of
ChromaDB, gated by `PEP_ORACLE_SERVE_FROM_ARTIFACT`. The OptiPlex keeps its default
(ChromaDB) because nothing rebuilds the artifact on ingest until Phase 3.

## Serve from the artifact locally

Requires a built artifact (see `docs/aws/phase1-backfill-runbook.md`) and Bedrock
creds (the query embedder must be Titan, matching the artifact):

```bash
export PEP_ORACLE_SERVE_FROM_ARTIFACT=1
export PEP_ORACLE_CORPUS_URI=~/.pep-oracle          # base; /corpus is appended
export PEP_ORACLE_EMBED_BACKEND=bedrock
export PEP_ORACLE_BEDROCK_REGION=ap-southeast-2
uv run pep-oracle-server      # or: uvicorn pep_oracle.server:app
```

Then:
```bash
curl -s localhost:8000/version | python -m json.tool   # corpus_version, embed_model, episode_range
```

If the active embedder doesn't match the artifact's `embed_model`, the first MCP
query raises a clear "query embedder mismatch" error (by design — bge-large queries
against a Titan corpus would be meaningless). `GET /version` surfaces the same via
`corpus_error` if the artifact can't be loaded.

## The Lambda entrypoint
`pep_oracle.server:handler` is the Mangum adapter (the same `app`); Phase 2c points
the Lambda at it. Locally it's unused — `uvicorn`/`pep-oracle-server` run the app directly.
````

- [ ] **Step 2: Add a CLAUDE.md bullet** — after the corpus-artifact bullet added in Phase 1, insert:

```markdown
- **Serving source seam** (`mcp_server.get_serving_corpus`): `PEP_ORACLE_SERVE_FROM_ARTIFACT=1` makes the MCP tool retrieve from the corpus artifact via `corpus.current_corpus()` (TTL-refreshed `InMemoryCorpus`, default 300s, atomic version swap); otherwise it uses the live ChromaDB collection (OptiPlex default). Artifact serving requires `EMBED_BACKEND=bedrock` with `EMBED_MODEL` matching the manifest (`corpus._validate_serving` enforces dims + embedder at load). `server.handler` is the Mangum Lambda adapter; `GET /version` reports code (semver + `GIT_SHA`) and corpus (version, episode_range, built_at, embed_model). Phase 2a of the AWS migration.
```

- [ ] **Step 3: Final full-suite run**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add docs/aws/phase2a-serving-local-smoke.md CLAUDE.md
git commit -m "docs(serving): Phase 2a local smoke + CLAUDE.md serving-seam note"
```

---

## Self-Review (completed by plan author)

**Spec coverage (serving-core slice of Sections 1–2 + the carry-forwards):**
- Same ASGI app runs local/CI/Lambda (Mangum) → Task 6. ✓
- MCP retrieval from the S3/local corpus artifact, no ChromaDB on that path → Task 5 (`get_serving_corpus`) + Phase 1 `InMemoryCorpus`. ✓
- Bounded-staleness warm refresh + atomic swap of `current.json` version → Task 4 (`current_corpus`), made effective by Task 1 (cache-key fix). ✓
- `GET /version` reporting both code + corpus version axes → Task 7. ✓
- Config-gated so the live OptiPlex is unchanged (ChromaDB default) → Task 2 (`SERVE_FROM_ARTIFACT` default off) + Task 5 default branch. ✓
- Carry-forward #1 (hybrid cache `(name, version)`) → Task 1. #2 (manifest dims validated) → Task 3 `_validate_serving`. #3 (query embedder matches artifact `embed_model`) → Task 3 `_validate_serving`, enforced on the serving load path (Task 4). ✓
- **Deferred (correctly out of 2a):** OAuth→DynamoDB, JWT signing backends, Cognito gate (Phase 2b); CloudFront/OAC, Function URL, real Cognito/DynamoDB/S3, KMS, IAM, CDK, deploy (Phase 2c). The existing SQLite/HS256 OAuth + the `_BearerAuthASGIWrapper` are untouched.

**Placeholder scan:** No TBD / "add validation" / bare "write tests" — every step has complete code, exact commands, and expected output.

**Type consistency:** `current_corpus(base, ttl_seconds, now)` / `reset_serving_cache()` / `load_manifest(base) -> (str, Manifest)` / `_validate_serving(corpus, base)` / `get_serving_corpus()` are defined once and used consistently across Tasks 3–7. `InMemoryCorpus.version`/`.embeddings` (from Phase 1) are read by `_validate_serving` and the `(name, version)` cache key. `Manifest` fields (`dims`, `embed_model`, `episode_range`, `built_at`) used in Tasks 3/7 match the Phase 1 dataclass. `config.SERVE_FROM_ARTIFACT`/`CORPUS_REFRESH_TTL_SECONDS`/`GIT_SHA`/`CORPUS_URI`/`EMBED_BACKEND`/`EMBED_MODEL` names match Task 2 + Phase 1 config.

---

## Execution Handoff

Phase 2a plan saved to `docs/superpowers/plans/2026-06-02-aws-migration-phase2a-serving-core.md`. Per the standing preference I'll execute it with **subagent-driven development** (fresh subagent per task, two-stage review) in an isolated worktree, unless you say otherwise. After 2a, the next plans are **2b** (OAuth→DynamoDB + JWT signing seam + Cognito gate) and **2c** (CDK infra + deploy).
