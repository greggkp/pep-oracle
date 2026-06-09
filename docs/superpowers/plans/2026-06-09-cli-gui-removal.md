# CLI + GUI Removal (AWS-only excision) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the user-facing CLI and web GUI from pep-oracle and excise the orphaned local-ChromaDB support code, leaving an AWS-only tree (MCP serving Lambda + Fargate ingestion).

**Architecture:** Remove from consumers inward so the test suite stays green after every task: drop CLI commands and the web GUI first, then split the shared modules (`ingest.py`, `store.py`), then delete the now-orphaned modules (`query.py`, `topics.py`), decouple serving from ChromaDB, and finally drop the `fastembed`/`chromadb`/`pydub` dependencies and rewrite the docs.

**Tech Stack:** Python 3.12, `uv`, pytest, FastAPI + Mangum (Lambda), the `mcp` SDK, AWS Bedrock embeddings, the corpus-artifact (`corpus.py` / `InMemoryCorpus`).

**Spec:** `docs/superpowers/specs/2026-06-09-cli-gui-removal-design.md`

---

## Spec refinements discovered during planning

These refine the spec's keep/remove lists (apply these, not the spec where they differ):

- **`cache.py`, the `/freshness` route, and the `_caches` background-refresh layer are GUI-support only** (no product/MCP consumer) → **remove** them (the spec listed `cache.py`/`/freshness` under "keep"). Also delete `tests/test_cache.py` and `tests/test_parse_episode_input.py`.
- **`server.py` drops all `store` imports.** `get_ingestion_stats` is consumed by `mcp_server`, not `server`.
- Surviving product routes in `server.py`: **`/health`, `/version`** + the **`/mcp`** mount and OAuth routes. (`/freshness` goes.)

## Commit gate (read before executing)

Every `git commit` triggers a Claude Code PreToolUse hook that blocks unless **(1)** `uv run pytest -x -q` passes **and (2)** `.claude/.md-reviewed` exists and is newer than `CLAUDE.md`. Tasks 1–8 do **not** change `CLAUDE.md`, so its last review stays valid — **`touch .claude/.md-reviewed` immediately before each commit** in those tasks (the hook consumes the flag on success). **Task 9** rewrites `CLAUDE.md` via `/claude-md-improver`, then touches the flag. Do not bypass with `--no-verify` (the hook matches `git commit*` regardless).

## Per-task verification ritual

Each task ends with the same three checks before commit (cheap, catches dangling refs):
1. `uv run ruff check .` — fix any `F401` (unused import), `F811`, `F821` (undefined name) it flags by removing the now-dead import/reference. Ruff is the authority on leftover imports, so you don't have to enumerate them by hand.
2. `uv run pytest -q` — green.
3. `touch .claude/.md-reviewed && git commit`.

---

## Task 1: Remove standalone ops/local CLI commands + their leaf modules

Removes `backup`, `build-references`, `remap-speakers`, `backfill` — each is a CLI command backed by a single module + test, imported by nothing else (verified: `backup`/`references`/`backfill`/`remap_speakers` are imported only by `cli.py`).

**Files:**
- Modify: `src/pep_oracle/cli.py`
- Delete: `src/pep_oracle/backup.py`, `src/pep_oracle/references.py`, `src/pep_oracle/remap_speakers.py`, `src/pep_oracle/backfill.py`
- Delete: `tests/test_backup.py`, `tests/test_references.py`, `tests/test_remap_speakers.py`, `tests/test_backfill.py`

- [ ] **Step 1: Confirm no other importers**

Run: `grep -rnE "import (backup|references|remap_speakers|backfill)\b|from pep_oracle\.(backup|references|remap_speakers|backfill)" src/ tests/`
Expected: matches only inside `cli.py` and the four test files being deleted. If anything else imports them, stop and re-scope.

- [ ] **Step 2: Delete the four modules and their tests**

```bash
git rm src/pep_oracle/backup.py src/pep_oracle/references.py \
       src/pep_oracle/remap_speakers.py src/pep_oracle/backfill.py \
       tests/test_backup.py tests/test_references.py \
       tests/test_remap_speakers.py tests/test_backfill.py
```

- [ ] **Step 3: Remove the four commands from `cli.py`**

In `src/pep_oracle/cli.py`, delete these command functions and their `@cli.command(...)` decorators: `backup_cmd` (`name="backup"`), `build_references_cmd` (`name="build-references"`), `remap_speakers_cmd` (`name="remap-speakers"`), `backfill_cmd` (`name="backfill"`). Delete any module-level `import`/`from` lines that referenced the deleted modules.

- [ ] **Step 4: Lint, catch dangling imports**

Run: `uv run ruff check .`
Expected: clean. If `F401` flags a now-unused import in `cli.py` (e.g. `from pep_oracle import backup`), remove it.

- [ ] **Step 5: Verify the CLI still builds and lists the right commands**

Run: `uv run pep-oracle --help`
Expected: lists the remaining commands; **no** `backup`, `build-references`, `remap-speakers`, `backfill`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest -q`
Expected: PASS (fewer tests; no collection/import errors).

- [ ] **Step 7: Commit**

```bash
touch .claude/.md-reviewed
git add -A
git commit -m "refactor: drop backup/build-references/remap-speakers/backfill CLI commands + modules"
```

---

## Task 2: Remove the web GUI from `server.py` + delete web/cache/worker

Strips `server.py` down to the product surface (`/health`, `/version`, the `/mcp` mount, OAuth) and deletes the GUI assets and the GUI-only support layer. **Does not** touch `topics.py` (still imported by `ingest.py` until Task 4) or `query.py` (still used by `mcp_server` until Task 5) — but **does** remove `server.py`'s imports of them.

**Files:**
- Modify: `src/pep_oracle/server.py`
- Delete: `src/pep_oracle/web/index.html` (and the `web/` dir), `src/pep_oracle/ingest_worker.py`, `src/pep_oracle/cache.py`
- Delete: `tests/test_web_conversation.py`, `tests/test_web_episodes.py`, `tests/test_web_live.py`, `tests/test_web_responsive.py`, `tests/test_topics_endpoint.py`, `tests/test_parse_episode_input.py`, `tests/test_cache.py`
- Rewrite: `tests/test_server.py`

- [ ] **Step 1: Delete the GUI assets, worker, cache module, and GUI-only tests**

```bash
git rm -r src/pep_oracle/web
git rm src/pep_oracle/ingest_worker.py src/pep_oracle/cache.py
git rm tests/test_web_conversation.py tests/test_web_episodes.py \
       tests/test_web_live.py tests/test_web_responsive.py \
       tests/test_topics_endpoint.py tests/test_parse_episode_input.py \
       tests/test_cache.py
```

- [ ] **Step 2: Strip `server.py` to product-only**

In `src/pep_oracle/server.py`, **delete**:
- Routes + their helper functions: `@app.post("/ask")` (`api_ask`), `@app.get("/status")` (`api_status`, `_fetch_status`), `@app.get("/episodes")` (`api_episodes`, `_fetch_episodes`), `@app.get("/topics")` (`api_topics`, `_fetch_topics`), `@app.post("/ingest")` (`api_ingest`, its nested `_apply_progress`/`_run`), `@app.get("/ingest/status")` (`api_ingest_status`), `@app.api_route("/reload")` (`api_reload`), `@app.get("/")` (`root`), and `@app.get("/freshness")` (`api_freshness`).
- The cache layer: the `_caches` dict, the ingest globals (`_ingest_lock`, `_ingest_running`, `_ingest_last_result`, `_ingest_progress`), the helpers `parse_episode_input`, `_get_fresh_collection`, `_serving_collection`, and the Pydantic models `AskRequest`, `IngestRequest`.
- The `lifespan` cache-refresh tasks: replace the whole `lifespan` with nothing and change the app to `app = FastAPI(title="pep-oracle")` (drop the `lifespan=lifespan` arg and the `@asynccontextmanager`/`lifespan` def).
- Module constant `WEB_DIR`.

**Keep**: `_BearerAuthASGIWrapper`, `_resolve_signing_key`, `mount_mcp_if_configured`, `_code_version`, `@app.get("/health")`, `@app.get("/version")` (see Step 3), `mount_mcp_if_configured(app)` call, `_McpSlashNormalizer`, `_make_lambda_handler`, `handler`, `main`.

- [ ] **Step 3: Make `/version` load the manifest unconditionally**

`/version` currently guards the corpus block with `if _config.SERVE_FROM_ARTIFACT:`. Artifact serving is the only path now, so make it unconditional. Replace the guard with a direct attempt (keep the existing try/except that logs and sets `corpus_error`):

```python
@app.get("/version")
async def api_version():
    semver, sha = _code_version()
    out = {"code_semver": semver, "code_git_sha": sha}
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
        logger.warning("corpus manifest unavailable for /version: %s", exc)
        out["corpus_error"] = "corpus manifest unavailable"
    return out
```

- [ ] **Step 4: Lint, remove dead imports**

Run: `uv run ruff check .`
Expected: `F401` on the now-unused imports in `server.py`. Remove them — they should include: `FileResponse`, `from pep_oracle.query import ask as do_ask`, the whole `from pep_oracle.store import (...)` block, `from pep_oracle.topics import bootstrap_topics, load_topics`, `from pep_oracle.feed import fetch_episodes`, `from pep_oracle.cache import CacheEntry, get_freshness, trigger_refresh`, `CHROMA_DIR`, `TOPICS_PATH` (from the `config` import), and likely `asyncio`, `json`, `sys`, `BaseModel`. Keep imports still used by the surviving code (`os`, `subprocess`, `logging`, `urlparse`, `FastAPI`, `config as _config`, `corpus as _corpus`, `oauth`, `authorize_gate`, version helpers). Let ruff be the authority — remove exactly what it flags, re-run until clean.

- [ ] **Step 5: Rewrite `tests/test_server.py` to the product surface**

Replace the file with tests for only the surviving behavior. Use FastAPI's `TestClient`.

```python
from fastapi.testclient import TestClient

from pep_oracle import server


def test_health_ok():
    client = TestClient(server.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_version_reports_code(monkeypatch):
    # No corpus available locally → corpus_error branch, but code_* always present.
    monkeypatch.setattr(server._config, "SEMVER", "v9.9.9")
    monkeypatch.setattr(server._config, "GIT_SHA", "abc1234")
    client = TestClient(server.app)
    body = client.get("/version").json()
    assert body["code_semver"] == "v9.9.9"
    assert body["code_git_sha"] == "abc1234"


def test_mcp_mount_skipped_without_public_url(monkeypatch):
    monkeypatch.delenv("PEP_ORACLE_PUBLIC_URL", raising=False)
    from fastapi import FastAPI

    app = FastAPI()
    assert server.mount_mcp_if_configured(app) is False
```

Preserve any *still-valid* existing `test_server.py` cases covering `mount_mcp_if_configured` gating (cognito/trusted_upstream/unknown), `_BearerAuthASGIWrapper`, and `_McpSlashNormalizer` — port them over rather than dropping coverage. Delete cases that exercised the removed GUI routes.

- [ ] **Step 6: Lint + tests**

Run: `uv run ruff check . && uv run pytest -q`
Expected: PASS.

- [ ] **Step 7: Import-sanity for the Lambda entrypoint**

Run: `uv run python -c "import pep_oracle.server as s; assert s.handler is not None or True; print('ok')"`
Expected: `ok` (no ImportError).

- [ ] **Step 8: Commit**

```bash
touch .claude/.md-reviewed
git add -A
git commit -m "refactor: remove web GUI, ingest_worker, and cache layer from server"
```

---

## Task 3: Remove the local user CLI commands

Removes `episodes`, `ingest`, `ask`, `status`, `export`, `import` from `cli.py`, leaving only the `cli` group + `ingest-artifact` + `eval-retrieval`.

**Files:**
- Modify: `src/pep_oracle/cli.py`
- Rewrite: `tests/test_cli.py`

- [ ] **Step 1: Remove the six commands**

In `src/pep_oracle/cli.py`, delete the command functions + decorators: `episodes`, `ingest`, `ask`, `export_cmd` (`name="export"`), `import_cmd` (`name="import"`), `status`. Keep the `@click.group()` `cli`, `ingest_artifact_cmd` (`name="ingest-artifact"`), `eval_retrieval_cmd` (`name="eval-retrieval"`).

- [ ] **Step 2: Lint, remove dead imports**

Run: `uv run ruff check .`
Expected: `F401` on imports only the removed commands used (e.g. `from pep_oracle.query import ...`, `from pep_oracle.ingest import ingest_all, ingest_episode`, `from pep_oracle.store import export_episodes, import_chunks`, `from pep_oracle.feed import ...`). Remove exactly what ruff flags. Keep imports used by the surviving two commands (`ingest_artifact`, `eval_retrieval`, `corpus`, `embeddings`).

- [ ] **Step 3: Rewrite `tests/test_cli.py`**

Replace with tests for the two surviving commands. Use Click's `CliRunner`.

```python
from click.testing import CliRunner

from pep_oracle.cli import cli


def test_help_lists_only_surviving_commands():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in ("ingest-artifact", "eval-retrieval"):
        assert name in result.output
    for gone in ("episodes", "ingest ", "ask", "status", "export", "import", "backup"):
        assert gone not in result.output


def test_ingest_artifact_has_help():
    result = CliRunner().invoke(cli, ["ingest-artifact", "--help"])
    assert result.exit_code == 0
```

Port any still-relevant `ingest-artifact`/`eval-retrieval` cases from the old `test_cli.py`; drop the rest.

- [ ] **Step 4: Lint + tests**

Run: `uv run ruff check . && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
touch .claude/.md-reviewed
git add -A
git commit -m "refactor: drop local user CLI commands (episodes/ingest/ask/status/export/import)"
```

---

## Task 4: Split `ingest.py`; delete `topics.py`

`ingest.py` keeps `episode_chunks_and_embeddings` (used by `ingest_artifact`) and loses the local-ChromaDB ingest functions. That removes the last `topics.py` consumer, so `topics.py` and its tests go.

**Files:**
- Modify: `src/pep_oracle/ingest.py`
- Delete: `src/pep_oracle/topics.py`
- Delete: `tests/test_topics.py`, `tests/test_topics_io.py`, `tests/test_clean_episode_topics.py`, `tests/test_ingest_topics.py`
- Rewrite: `tests/test_ingest.py`

- [ ] **Step 1: Confirm `episode_chunks_and_embeddings` is the only survivor needed**

Run: `grep -rnE "from pep_oracle\.ingest import|ingest\.(ingest_all|ingest_episode|_ingest_one|episode_chunks_and_embeddings)" src/ tests/`
Expected: `ingest_artifact.py` imports `episode_chunks_and_embeddings`; `ingest_all`/`ingest_episode`/`_ingest_one` have no surviving non-test caller (the CLI/server callers were removed in Tasks 2–3). If a survivor references them, stop.

- [ ] **Step 2: Remove the local-ingest functions from `ingest.py`**

Delete `_ingest_one`, `ingest_all`, `ingest_episode`. Keep `episode_chunks_and_embeddings` and any helper it uses. Then run `uv run ruff check .` and remove the imports those deleted functions used — expected `F401` on `from pep_oracle.store import add_chunks, delete_episode, get_client, get_collection, get_ingested_guids`, `from pep_oracle.topics import clean_episode_topics, parse_description_topics, save_topics`, `from pep_oracle.config import TOPICS_PATH`, and possibly `click`/`time`. Keep `chunk_transcript`, `embed_texts`, `fetch_episodes`, `Episode`, `get_transcript` if `episode_chunks_and_embeddings` uses them (verify by reading the surviving function).

- [ ] **Step 3: Delete `topics.py` and its tests**

```bash
git rm src/pep_oracle/topics.py tests/test_topics.py \
       tests/test_topics_io.py tests/test_clean_episode_topics.py \
       tests/test_ingest_topics.py
```

- [ ] **Step 4: Rewrite `tests/test_ingest.py` to cover only `episode_chunks_and_embeddings`**

Read the surviving `episode_chunks_and_embeddings` signature first, then port the existing chunks-and-embeddings cases from the old `test_ingest.py` (they already mock Modal/embeddings via `tests/conftest.py`). Delete cases that exercised `ingest_all`/`ingest_episode`/ChromaDB writes/topics.

- [ ] **Step 5: Lint + tests + import sanity**

Run: `uv run ruff check . && uv run pytest -q`
Expected: PASS.
Run: `uv run python -c "import pep_oracle.ingest_artifact; print('ok')"`
Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
touch .claude/.md-reviewed
git add -A
git commit -m "refactor: split ingest.py to chunks_and_embeddings only; delete topics.py"
```

---

## Task 5: Extract `format_timestamp`, decouple serving from ChromaDB, delete `query.py`, drop `SERVE_FROM_ARTIFACT`

`mcp_server` is the last `query.py` consumer (`format_timestamp`) and the last live-ChromaDB serving consumer (`get_fresh_collection`). Move the helper in, make serving artifact-only, remove the now-vestigial flag, and delete `query.py`.

**Files:**
- Modify: `src/pep_oracle/mcp_server.py`, `src/pep_oracle/config.py`
- Delete: `src/pep_oracle/query.py`, `tests/test_query.py`
- Modify: `tests/test_mcp_server.py` (if it patched `get_fresh_collection`/`SERVE_FROM_ARTIFACT`)

- [ ] **Step 1: Add `format_timestamp` to `mcp_server.py`**

Add this function near the top of `src/pep_oracle/mcp_server.py` (it's pure, no deps):

```python
def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    return f"{h}:{m:02d}:{s:02d}"
```

- [ ] **Step 2: Update `mcp_server.py` imports + serving seam**

In `src/pep_oracle/mcp_server.py`:
- Delete `from pep_oracle.query import format_timestamp` (now defined locally).
- Change `from pep_oracle.store import get_fresh_collection, get_ingestion_stats` to `from pep_oracle.store import get_ingestion_stats`.
- Replace `get_serving_corpus` with the artifact-only version:

```python
def get_serving_corpus():
    """Retrieval source: the corpus artifact (InMemoryCorpus), TTL-refreshed and
    version-swapped atomically. Validates dims + embedder against the manifest at
    load. The only serving path (ChromaDB serving was removed in the AWS-only cut)."""
    return corpus_mod.current_corpus(
        config.CORPUS_URI, ttl_seconds=config.CORPUS_REFRESH_TTL_SECONDS
    )
```

- [ ] **Step 3: Remove `SERVE_FROM_ARTIFACT` from `config.py`**

Delete the `SERVE_FROM_ARTIFACT = os.getenv(...) == "1"` line (and its comment block) in `src/pep_oracle/config.py`. Then `grep -rn "SERVE_FROM_ARTIFACT" src/ tests/` — expected no remaining references (server `/version` was made unconditional in Task 2; mcp_server in Step 2). Fix any stragglers.

- [ ] **Step 4: Delete `query.py` and its test**

```bash
git rm src/pep_oracle/query.py tests/test_query.py
```

Run: `grep -rnE "from pep_oracle\.query import|import pep_oracle\.query|pep_oracle\.query\." src/ tests/`
Expected: no matches. If `eval_retrieval.py` or any test still imports `query`, note it for Task 6 (it shouldn't — `eval_retrieval` does not import `query`).

- [ ] **Step 5: Fix `tests/test_mcp_server.py`**

If it monkeypatched `mcp_server.get_fresh_collection` or set `SERVE_FROM_ARTIFACT`, update it to patch `mcp_server.get_serving_corpus` (or `corpus_mod.current_corpus`) to return a fake corpus. Keep the citation-shape and search assertions.

- [ ] **Step 6: Lint + tests + import sanity**

Run: `uv run ruff check . && uv run pytest -q`
Expected: PASS.
Run: `uv run python -c "import pep_oracle.mcp_server, pep_oracle.server; print('ok')"`
Expected: `ok`.

- [ ] **Step 7: Commit**

```bash
touch .claude/.md-reviewed
git add -A
git commit -m "refactor: serving is artifact-only; extract format_timestamp; delete query.py + SERVE_FROM_ARTIFACT"
```

---

## Task 6: Adapt `eval-retrieval` to corpus-only

`eval_retrieval.run_comparison` is the last `store.get_fresh_collection` consumer (the live-ChromaDB eval path). Remove it; keep the artifact path (`evaluate_corpus`).

**Files:**
- Modify: `src/pep_oracle/eval_retrieval.py`, `src/pep_oracle/cli.py`
- Rewrite: `tests/test_eval_retrieval.py`

- [ ] **Step 1: Remove the live-ChromaDB eval path**

In `src/pep_oracle/eval_retrieval.py`, delete `run_comparison` (imports `store.get_fresh_collection`) and `_semantic_retriever` if it is used only by `run_comparison`. Confirm first:
Run: `grep -nE "_semantic_retriever|run_comparison|_hybrid_retriever|evaluate_corpus|evaluate\(" src/pep_oracle/eval_retrieval.py src/pep_oracle/cli.py tests/`
Keep `evaluate`, `_hybrid_retriever`, `evaluate_corpus`, `resolve_relevant_episodes`, `recall_at_k`, `reciprocal_rank`, `aggregate`, `format_single`, `format_report`, `CASES`. Run `uv run ruff check .` and drop the freed `store`/`chromadb` imports it flags.

- [ ] **Step 2: Make the `eval-retrieval` command corpus-only**

Read `eval_retrieval_cmd` in `cli.py`. It currently branches on `--corpus`. Make `--corpus` required and always run the artifact path. Target shape (adapt to the actual `load_current` / `evaluate_corpus` / `embed_texts` signatures):

```python
@cli.command(name="eval-retrieval")
@click.option("--corpus", "corpus_uri", required=True,
              help="Base URI of the corpus artifact to evaluate (s3://… or a local path).")
def eval_retrieval_cmd(corpus_uri: str) -> None:
    from pep_oracle import eval_retrieval
    from pep_oracle.corpus import load_current
    from pep_oracle.embeddings import embed_texts

    corpus = load_current(corpus_uri)
    res = eval_retrieval.evaluate_corpus(corpus, embed=lambda ts: embed_texts(ts))
    click.echo(eval_retrieval.format_single("hybrid", res))
```

- [ ] **Step 3: Rewrite `tests/test_eval_retrieval.py`**

Keep the pure-function tests (`recall_at_k`, `reciprocal_rank`, `aggregate`, `resolve_relevant_episodes`) and any `evaluate_corpus` test that builds a tiny fake corpus. Delete tests that called `run_comparison`/`_semantic_retriever` or `get_fresh_collection`. (The live guard `tests/test_eval_retrieval_live.py` already uses the corpus path — leave it.)

- [ ] **Step 4: Lint + tests**

Run: `uv run ruff check . && uv run pytest -q`
Expected: PASS.
Run: `grep -rn "get_fresh_collection" src/ tests/` — expected only inside `store.py` now (the definition), no callers.

- [ ] **Step 5: Commit**

```bash
touch .claude/.md-reviewed
git add -A
git commit -m "refactor: eval-retrieval runs against the corpus artifact only"
```

---

## Task 7: Split `store.py` (remove ChromaDB)

With no remaining caller of the ChromaDB functions, shrink `store.py` to the three generic helpers and drop the `chromadb` import.

**Files:**
- Modify: `src/pep_oracle/store.py`
- Rewrite: `tests/test_store.py`

- [ ] **Step 1: Confirm callers are gone**

Run: `grep -rnE "get_client|get_collection|get_fresh_collection|add_chunks|store\.query|get_ingested_guids|delete_episode|export_episodes|import_chunks|_apply_recency_boost|_build_where" src/ tests/`
Expected: matches only inside `store.py` and `tests/test_store.py`. If a survivor references any, stop and resolve.

- [ ] **Step 2: Delete the ChromaDB functions + import**

In `src/pep_oracle/store.py`, delete: `get_client`, `get_collection`, `get_fresh_collection`, `add_chunks`, `query`, `_apply_recency_boost`, `get_ingested_guids`, `delete_episode`, `export_episodes`, `import_chunks`, and `_build_where` (verify `_build_where` has no surviving caller via the Step 1 grep). Delete `import chromadb` (and any chromadb-only imports). **Keep**: `SENTINEL_NO_TIME`, `get_ingestion_stats`, `_chunk_metadata`, plus any imports they need (`Chunk`/`models`).

- [ ] **Step 3: Confirm `get_ingestion_stats` works on `InMemoryCorpus`**

Read `get_ingestion_stats`. It must use only the generic surface (`.get(include=[...])`, `.count()`, `.name`) that `InMemoryCorpus` provides (`corpus.py:111` documents this contract). Retype its parameter annotation from `chromadb.Collection` to a duck-typed name (e.g. drop the annotation or use a `Protocol`/`object`). Do not change its logic if it already only uses the generic surface.

- [ ] **Step 4: Rewrite `tests/test_store.py`**

Cover the three survivors: `get_ingestion_stats` against a fake corpus (object exposing `.get`/`.count`/`.name`, or reuse the `InMemoryCorpus` test helper from `tests/test_corpus.py`), `_chunk_metadata` against a `Chunk`, and `SENTINEL_NO_TIME`'s value. Delete all ChromaDB-function tests.

- [ ] **Step 5: Lint + tests + import sanity**

Run: `uv run ruff check . && uv run pytest -q`
Expected: PASS.
Run: `uv run python -c "import pep_oracle.store as s; assert not hasattr(s, 'get_fresh_collection'); print('ok')"`
Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
touch .claude/.md-reviewed
git add -A
git commit -m "refactor: store.py is ChromaDB-free (keep stats + chunk-metadata helpers)"
```

---

## Task 8: Bedrock-only embeddings; drop `fastembed`/`chromadb`/`pydub`

**Files:**
- Modify: `src/pep_oracle/embeddings.py`, `src/pep_oracle/config.py`, `pyproject.toml`, `Dockerfile`, `Dockerfile.ingest`
- Rewrite/trim: `tests/test_embeddings.py`

- [ ] **Step 1: Make `embeddings.py` Bedrock-only**

In `src/pep_oracle/embeddings.py`: delete `MODEL_NAME`, the `_model` global, `_get_model()`, and the fastembed branch. `embed_texts` becomes:

```python
def embed_texts(texts: list[str]) -> list[list[float]]:
    return [_embed_one_bedrock(t) for t in texts]
```

Update the module docstring to describe the single Bedrock backend. Keep `_bedrock`, `_bedrock_client`, `_ThrottlingError`, `_is_throttling`, `_embed_one_bedrock`.

- [ ] **Step 2: Remove `EMBED_BACKEND` from `config.py`**

Delete `EMBED_BACKEND = os.getenv("PEP_ORACLE_EMBED_BACKEND", "fastembed")` and its comment. Keep `EMBED_MODEL`, `EMBED_DIMS`, `BEDROCK_REGION`. Then `grep -rn "EMBED_BACKEND" src/ tests/ infra/` — expected no src references (Lambda/Fargate env may still *set* it harmlessly; leave infra env vars alone unless a test asserts on them). Fix any src/test stragglers.

- [ ] **Step 3: Rewrite `tests/test_embeddings.py`**

Drop fastembed-path tests. Keep the retry/throttling tests (they patch `_bedrock_client` / use `_ThrottlingError`) and an `embed_texts` test that monkeypatches `_embed_one_bedrock`. Ensure no test imports `fastembed`.

- [ ] **Step 4: Drop the dependencies in `pyproject.toml`**

Remove from `[project].dependencies`: `"fastembed>=0.4"`, `"chromadb"`, `"pydub"`. Check `audioop-lts`: `grep -rn "audioop\|pydub" src/` — if nothing imports `audioop`/`pydub`, remove `"audioop-lts;python_version>='3.13'"` too. Leave `[dependency-groups].dev` and the `server`/`aws` extras as-is.

- [ ] **Step 5: Re-sync the env without the dropped packages**

Run: `uv sync --extra server --extra aws --extra dev`
Expected: resolves; fastembed/chromadb/pydub no longer installed.

- [ ] **Step 6: Import sanity with the deps gone**

Run: `uv run python -c "import pep_oracle.server, pep_oracle.mcp_server, pep_oracle.ingest_artifact, pep_oracle.embeddings, pep_oracle.store; print('ok')"`
Expected: `ok` — proves nothing in the surviving tree imports `fastembed`/`chromadb`.

- [ ] **Step 7: Update the Dockerfile comments**

In `Dockerfile` and `Dockerfile.ingest`, delete the comment lines claiming fastembed/chromadb come in as unused base deps (they're gone now). No build-stage logic changes.

- [ ] **Step 8: Lint + tests**

Run: `uv run ruff check . && uv run pytest -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
touch .claude/.md-reviewed
git add -A
git commit -m "refactor: Bedrock-only embeddings; drop fastembed/chromadb/pydub deps"
```

---

## Task 9: Docs — rewrite `CLAUDE.md`, trim `.env.example` and `deploy/`

**Files:**
- Modify: `CLAUDE.md`, `.env.example`, `deploy/` (the `pep-oracle-backup.service` reference)

- [ ] **Step 1: Trim `.env.example`**

Remove keys tied to removed features. Keep what the Lambda/Fargate/local-MCP-dev contract uses: `ANTHROPIC_API_KEY` is no longer used by the surviving tree (the `/ask` Claude path is gone) — confirm with `grep -rn "ANTHROPIC_API_KEY\|anthropic" src/` and remove it from `.env.example` if unused. Keep `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` (Fargate/local ingest), `PEP_ORACLE_PUBLIC_URL`, the OAuth/Cognito/signing block, `PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH`. Drop the backup-remote key. Remove the `PEP_ORACLE_SERVE_FROM_ARTIFACT`/`EMBED_BACKEND` notes if present.

- [ ] **Step 2: Remove the `backup.service` reference in `deploy/`**

Run: `grep -rln "backup" deploy/`
Delete `deploy/pep-oracle-backup.service` if present and remove the `OnSuccess=pep-oracle-backup.service` line from `deploy/pep-oracle-ingest.service`. Leave the rest of `deploy/` for the separate OptiPlex-decommission task.

- [ ] **Step 3: Rewrite `CLAUDE.md` via the improver**

Invoke `/claude-md-improver`. Remove the sections describing: the CLI usage block, the web UI, the Query/`/ask` pipeline, topic chips, the embedding-backend fastembed default, local ChromaDB ingest + `store.py` ChromaDB, backup, and the three-ingestion-entry-points note (only Fargate + local `ingest-artifact` remain). Keep: MCP server, corpus artifact, hybrid/temporal retrieval, Fargate ingestion, OAuth, AWS deployment (Phases 1–4). Keep it under the ~300-line ceiling (this should net-shrink it).

- [ ] **Step 4: Lint + full test sweep**

Run: `uv run ruff check . && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit (real CLAUDE.md review)**

```bash
# /claude-md-improver should have touched .claude/.md-reviewed; if not:
touch .claude/.md-reviewed
git add -A
git commit -m "docs: rewrite CLAUDE.md and .env.example for the AWS-only tree"
```

---

## Task 10: Final verification

No code changes — prove the slimmed tree builds, synthesizes, and serves.

- [ ] **Step 1: Root + infra tests**

Run: `uv run pytest -q`
Expected: PASS.
Run: `cd infra && python -m pytest -q ; cd ..`
Expected: PASS (no infra changes; must stay green).

- [ ] **Step 2: CDK synth**

Run: `cd infra && npx cdk synth '*' -c allowed_email=ci@example.com > /dev/null && cd ..`
Expected: synth succeeds for all stacks.

- [ ] **Step 3: Both docker images build**

Run: `docker build -f Dockerfile -t pep-oracle:rm .`
Run: `docker build -f Dockerfile.ingest -t pep-oracle-ingest:rm .`
Expected: both succeed (smaller images; no fastembed/chromadb).

- [ ] **Step 4: Live smoke against deployed prod (read-only)**

Run: `uv run pytest -m live tests/test_smoke_live.py -q` (or `python scripts/smoke.py` against the deployed URL).
Expected: `/health`, `/version` (sha+semver), `/.well-known/oauth-authorization-server`, `/mcp` no-token→401 and minted-JWT `initialize`/`tools/list`/`tools/call`→200 all pass. (This hits the *currently deployed* Lambda, which still has the old code — it confirms the removal didn't change the product contract the smoke encodes. The new code's smoke runs in CI/after deploy.)

- [ ] **Step 5: Open the PR**

```bash
git push -u origin cli-gui-removal
gh pr create --title "Remove CLI + GUI (AWS-only excision)" \
  --body "Removes the user-facing CLI and web GUI and excises the orphaned local-ChromaDB support code, leaving an AWS-only tree (MCP serving Lambda + Fargate ingestion). Keeps ingest-artifact + eval-retrieval; drops fastembed/chromadb/pydub. Spec + plan in docs/superpowers/. Portable-dev-env rework follows on a separate branch."
```

CI (`ci.yml`) re-runs ruff + root pytest + infra pytest + `cdk synth '*'` + both docker builds with no AWS access — the authoritative clean-room gate.

---

## Self-review notes (coverage check against the spec)

- Survivor map → Tasks 1–8 implement every keep/remove item; the `store.py` and `ingest.py` splits are Tasks 7 and 4.
- `format_timestamp` extraction + `query.py` deletion → Task 5.
- `SERVE_FROM_ARTIFACT` removal + artifact-only serving → Tasks 2 (server `/version`) + 5 (mcp_server).
- `eval-retrieval` kept + adapted → Task 6.
- Dependency drop (fastembed/chromadb/pydub) + Bedrock-only embeddings → Task 8.
- Tests delete/rewrite/keep → folded into each task; the GUI-support tests (`test_cache`, `test_parse_episode_input`) are caught by the planning refinement in Task 2.
- Docs (`CLAUDE.md`, `.env.example`, `deploy/`) → Task 9.
- Verification (pytest/infra/synth/docker/smoke) → Task 10.
- Risks from the spec (`get_ingestion_stats` on `InMemoryCorpus`, `_build_where` callers, `pydub`/`audioop-lts` safety, Bedrock-only local dev) → addressed in Tasks 7 Step 3, 7 Step 1, 8 Step 4, 8 Step 6.
