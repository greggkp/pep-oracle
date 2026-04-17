# Local fastembed embeddings — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop the OpenAI dependency by generating embeddings locally with `fastembed` + `BAAI/bge-large-en-v1.5`, so pep-oracle talks only to Modal (transcription/diarization) and Anthropic (query LLM).

**Architecture:** Replace `embed_texts()` in `src/pep_oracle/embeddings.py` with a lazy-singleton `fastembed.TextEmbedding` wrapper. Drop the `openai_client` plumbing from `query.py`. The dimension change (1536 → 1024) is incompatible with the existing Chroma collection, so deploy includes a one-time export → drop collection → re-ingest step.

**Tech Stack:** Python 3.11+, `fastembed>=0.4` (ONNX runtime, ≈150 MB), `BAAI/bge-large-en-v1.5` weights (≈1.3 GB on first run, cached in `~/.cache/fastembed/`), existing ChromaDB + Anthropic stack.

**Spec:** `docs/superpowers/specs/2026-04-17-local-fastembed-embeddings-design.md`

---

## File structure

| File | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | modify | Add `fastembed`, remove `openai` dep |
| `src/pep_oracle/embeddings.py` | rewrite | Lazy-singleton bge-large wrapper; `embed_texts(list[str]) -> list[list[float]]` |
| `src/pep_oracle/config.py` | modify | Remove the now-unused `EMBEDDING_MODEL = "text-embedding-3-small"` constant |
| `src/pep_oracle/query.py` | modify | Drop `openai_client` param from `ask()`; drop `client=` kwarg from `embed_texts` call |
| `tests/test_embeddings.py` | rewrite | Single integration test: shape, non-zero, distinct vectors |
| `CLAUDE.md` | modify | Swap "Embedding batches of 20" bullet for fastembed note; remove `OPENAI_API_KEY` from env section |

No file deletions. No new files.

---

## Task 1: Swap dependencies — add fastembed, remove openai

**Files:**
- Modify (via `uv`): `pyproject.toml`, `uv.lock`

- [ ] **Step 1: Add fastembed**

Run: `uv add 'fastembed>=0.4'`
Expected: updates `pyproject.toml` (adds `fastembed>=0.4` to `dependencies`), updates `uv.lock`, installs fastembed + ONNX runtime transitive deps into the venv. Exit 0.

- [ ] **Step 2: Remove openai**

Run: `uv remove openai`
Expected: updates `pyproject.toml` (removes `openai`), updates `uv.lock`, uninstalls `openai` from the venv. Exit 0.

- [ ] **Step 3: Verify**

Run: `uv pip list | grep -E '^(openai|fastembed) '`
Expected: one matching line, `fastembed  0.4.x` or later. No `openai` line.

- [ ] **Step 4: Do NOT commit yet**

The repo is currently broken — `embeddings.py` still imports `openai`. The commit happens at the end of Task 2, in the same commit as the new `embeddings.py`. Task 2 will `git add pyproject.toml uv.lock` alongside the code changes.

---

## Task 2: Rewrite `embeddings.py` and its test

**Files:**
- Rewrite: `src/pep_oracle/embeddings.py`
- Rewrite: `tests/test_embeddings.py`
- Modify: `src/pep_oracle/config.py`

### TDD

- [ ] **Step 1: Write the new failing test**

Replace the entire contents of `tests/test_embeddings.py` with:

```python
from pep_oracle.embeddings import embed_texts


def test_embed_texts_returns_expected_shape_and_distinct_vectors():
    """Integration test: loads bge-large-en-v1.5 (≈1.3 GB on first run, cached).

    First run downloads the model into ~/.cache/fastembed and may take
    10-30s. Subsequent runs are fast (sub-second).
    """
    result = embed_texts(["hello world", "goodbye world"])

    assert len(result) == 2
    assert len(result[0]) == 1024
    assert len(result[1]) == 1024
    # Non-zero embeddings
    assert any(v != 0.0 for v in result[0])
    assert any(v != 0.0 for v in result[1])
    # Distinct inputs produce distinct embeddings
    assert result[0] != result[1]
```

- [ ] **Step 2: Run it and verify it fails**

Run: `uv run pytest tests/test_embeddings.py -v`
Expected: ImportError / AttributeError / test failure — the current `embed_texts` takes a `client=` kwarg and imports `openai` which is no longer installed.

- [ ] **Step 3: Rewrite `src/pep_oracle/embeddings.py`**

Replace the entire contents of `src/pep_oracle/embeddings.py` with:

```python
from fastembed import TextEmbedding

MODEL_NAME = "BAAI/bge-large-en-v1.5"

_model: TextEmbedding | None = None


def _get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in _get_model().embed(texts)]
```

No retry logic (fastembed is local — no rate limits). No batch-of-20 (fastembed batches internally). No `client` parameter.

- [ ] **Step 4: Remove dead `EMBEDDING_MODEL` constant from `config.py`**

Edit `src/pep_oracle/config.py`. Delete this line (currently line 22):

```python
EMBEDDING_MODEL = "text-embedding-3-small"
```

Leave `CHROMA_COLLECTION`, `QUERY_MODEL`, and everything else alone.

- [ ] **Step 5: Run the embedding test**

Run: `uv run pytest tests/test_embeddings.py -v`
Expected: PASS. First run downloads bge-large into `~/.cache/fastembed/` (10-30s on a typical home connection); subsequent runs are sub-second.

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest -x -q`
Expected: All tests pass. The existing `patch("pep_oracle.ingest.embed_texts", ...)` and `patch("pep_oracle.query.embed_texts", ...)` patterns in `test_ingest.py`, `test_query.py`, `test_ingest_topics.py` all keep working — they mock the symbol, not the underlying implementation.

If `test_query.py` or `test_ingest.py` fails due to something else, inspect before proceeding.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/pep_oracle/embeddings.py src/pep_oracle/config.py tests/test_embeddings.py uv.lock
git commit -m "feat: local embeddings via fastembed (bge-large-en-v1.5)

Replaces OpenAI text-embedding-3-small (1536d) with local
BAAI/bge-large-en-v1.5 (1024d) via the fastembed ONNX runtime.
Drops the openai dependency entirely. No rate limits, no batch
management — fastembed batches internally.

Requires a one-time re-ingest because Chroma collection dim changes."
```

---

## Task 3: Drop `openai_client` plumbing from `query.py`

**Files:**
- Modify: `src/pep_oracle/query.py`

- [ ] **Step 1: Remove the `openai_client` parameter**

Edit `src/pep_oracle/query.py`. In the `ask()` function signature (currently around line 216), remove the `openai_client=None,` parameter line. The new signature:

```python
def ask(
    question: str,
    top_k: int = 10,
    model: str = QUERY_MODEL,
    anthropic_client: anthropic.Anthropic | None = None,
    history: list[dict] | None = None,
) -> str:
```

- [ ] **Step 2: Remove the `client=` kwarg from the `embed_texts` call**

In the same file, change (currently line 235):

```python
    query_embedding = embed_texts([filters["search_query"]], client=openai_client)[0]
```

to:

```python
    query_embedding = embed_texts([filters["search_query"]])[0]
```

- [ ] **Step 3: Verify no other caller passes `openai_client`**

Run: `grep -rn "openai_client" src/ tests/`
Expected: zero matches. (Spot-checked earlier: `cli.py` and `server.py` never pass it — they call `ask(question=..., anthropic_client=..., ...)` positionally-by-name and `openai_client` was always a defaulted kwarg.)

If any match remains, remove the argument from that call site.

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest -x -q`
Expected: PASS. `test_query.py` already patches `pep_oracle.query.embed_texts` directly; it never supplies `openai_client`.

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/query.py
git commit -m "refactor: drop openai_client param from query.ask

embed_texts is now local (fastembed) — no client needed."
```

---

## Task 4: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the "Key design decisions" bullet about embedding batches**

Find the line in `CLAUDE.md`:

```
- **Embedding batches of 20** to stay within OpenAI's 40k TPM rate limit on lower-tier plans.
```

Replace with:

```
- **Local embeddings**: `embeddings.py` loads `BAAI/bge-large-en-v1.5` via `fastembed` as a lazy singleton. First use downloads ≈1.3 GB of ONNX weights to `~/.cache/fastembed/`; subsequent loads are ≈5s (cold process) or free (warm). Output is 1024-dim; any migration that changes the embedding model must drop-and-recreate the Chroma collection to match.
```

- [ ] **Step 2: Update the "Environment" section**

Find:

```
Required in `.env` (loaded via python-dotenv):
- `OPENAI_API_KEY` — for embeddings (`text-embedding-3-small`)
- `ANTHROPIC_API_KEY` — for Claude query responses
- `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` — Modal credentials for cloud transcription and diarization
```

Replace with:

```
Required in `.env` (loaded via python-dotenv):
- `ANTHROPIC_API_KEY` — for Claude query responses
- `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` — Modal credentials for cloud transcription and diarization

No `OPENAI_API_KEY` — embeddings are now generated locally via `fastembed`.
```

- [ ] **Step 3: Update the "Ingestion" architecture sentence**

Find (currently in the Architecture section):

```
→ `embeddings.py` (OpenAI batched) → `store.py` (ChromaDB upsert).
```

Replace with:

```
→ `embeddings.py` (local fastembed / bge-large) → `store.py` (ChromaDB upsert).
```

- [ ] **Step 4: Update the "Query" architecture sentence**

Find:

```
→ embed search query (OpenAI)
```

Replace with:

```
→ embed search query (local fastembed)
```

- [ ] **Step 5: Run claude-md-improver gate**

The pre-commit hook requires `.claude/.md-reviewed` to be touched after CLAUDE.md edits. Run the improver or, if no further changes are needed, touch the sentinel:

```bash
touch .claude/.md-reviewed
```

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md .claude/.md-reviewed
git commit -m "docs: CLAUDE.md updates for local fastembed embeddings"
```

---

## Task 5: Live migration — rebuild Chroma collection

This is a runbook, not code. It runs **once**, on the deployed box (`/opt/pep-oracle/app`), after Tasks 1–4 are merged and the server is restarted. All chunks in `~/.pep-oracle/chroma/` were embedded at 1536-dim with text-embedding-3-small and are incompatible with bge-large (1024-dim).

**Files:**
- Read: `~/.pep-oracle/cache/transcripts/*.whisper.json` (already on disk)
- Read: `~/.pep-oracle/cache/diarization/*.json` (already on disk)
- Rebuild: `~/.pep-oracle/chroma/`

- [ ] **Step 1: Stop the web server and disable the ingest timer**

```bash
sudo systemctl stop pep-oracle-api.service
sudo systemctl stop pep-oracle-ingest.timer
```

(The timer could fire mid-migration and wedge on a dimension mismatch. Stop it until we're done.)

- [ ] **Step 2: Back up the current collection to JSON**

```bash
uv run pep-oracle export "$HOME/pep-backup-pre-fastembed-$(date +%F).json"
```

Expected: writes a JSON file with all existing chunks + metadata + (old) embeddings. This is the rollback artifact.

- [ ] **Step 3: Verify transcript and diarization caches are present**

```bash
ls ~/.pep-oracle/cache/transcripts/*.whisper.json | wc -l
ls ~/.pep-oracle/cache/diarization/*.json | wc -l
```

Expected: ≈48 transcripts and ≈48 diarizations (one per ingested episode). If a number is much lower, STOP — the re-ingest will hit Modal for the missing ones, costing time and dollars.

- [ ] **Step 4: Drop the Chroma collection**

```bash
uv run python -c "from pep_oracle.store import get_client; get_client().delete_collection('pep_oracle')"
```

Expected: returns silently. The collection is gone; next access will recreate it (empty).

- [ ] **Step 5: Sanity-check that the collection is empty**

```bash
uv run pep-oracle status
```

Expected: 0 episodes, 0 chunks (the collection is recreated lazily; `status` will either show it empty or a fresh-collection message).

- [ ] **Step 6: Re-ingest everything from cached transcripts**

```bash
uv run pep-oracle ingest --force
```

Expected behaviour: for each of the ≈48 episodes, `get_transcript` loads from `~/.pep-oracle/cache/transcripts/<guid>.whisper.json` (no Modal call fires), diarization loads from `~/.pep-oracle/cache/diarization/<guid>.json` (no Modal call fires), chunks are embedded on the local CPU via bge-large, stored in Chroma. Wall-clock rough estimate: 5–15 min total on a modern desktop CPU. If the process prints "transcribing (Modal)" for any episode, STOP — the transcript cache is missing and you need to investigate before letting it hit Modal.

- [ ] **Step 7: Verify episode count matches the backup**

```bash
uv run pep-oracle status
```

Expected: same episode count as before (≈48) and a chunk count in the same ballpark.

- [ ] **Step 8: Sanity-query**

```bash
uv run pep-oracle ask "what's the latest on tariffs?"
uv run pep-oracle ask "who is Dr Dave?"
```

Expected: answers cite recent and older episodes respectively. If retrieval looks broken (empty results, irrelevant citations), the bge-large embeddings may be materially worse for this domain than expected — fall back to the JSON backup via `uv run pep-oracle import ~/pep-backup-pre-fastembed-<date>.json` after reverting the code, and escalate.

- [ ] **Step 9: Restart services**

```bash
sudo systemctl start pep-oracle-api.service
sudo systemctl start pep-oracle-ingest.timer
curl -s http://localhost:8000/health
```

Expected: `{"status":"ok"}`. Open the web UI and run one query to confirm end-to-end.

- [ ] **Step 10: Archive the backup**

Move `~/pep-backup-pre-fastembed-<date>.json` to wherever long-term backups live (or leave it in `$HOME` for a week before cleaning up). Do not commit it.

**Rollback (if Step 8 shows unacceptable retrieval quality):**

```bash
git revert <last-3-commits-from-this-plan>
uv pip install -e .
uv run python -c "from pep_oracle.store import get_client; get_client().delete_collection('pep_oracle')"
uv run pep-oracle import ~/pep-backup-pre-fastembed-<date>.json
sudo systemctl restart pep-oracle-api.service
```

---

## Verification checklist (after all tasks)

- [ ] `uv pip list | grep -E '^(openai|fastembed) '` → only `fastembed`.
- [ ] `grep -rn "openai" src/ tests/` → zero matches (or only comments/docstrings if any).
- [ ] `uv run pytest -x -q` → all pass.
- [ ] `pep-oracle ask "..."` returns sensible answers against the rebuilt collection.
- [ ] `/health` returns ok; web UI loads and queries work.
- [ ] Ingest timer fires at its next scheduled slot and processes zero or more new episodes without interactive prompts.

---

## Risks (carried from spec)

- **Retrieval quality regression**: bge-large is benchmarked slightly above text-embedding-3-small on MTEB, but the two models will place different things near each other in their respective vector spaces. The Step-8 sanity queries are the gate; rollback path is in place.
- **First-run model download**: ≈1.3 GB to `~/.cache/fastembed/`. One-time. Any CI host that doesn't persist `~/.cache/` will re-download every run; not a concern on the OptiPlex.
- **CPU ingest latency**: bge-large on CPU → ≈50-100 chunks/sec; a 300-chunk episode adds a few seconds of embed time on top of a 5–10 min Modal transcription. Imperceptible.
- **Feed rollover during migration**: if any of the 48 ingested episodes has dropped off the 100-episode RSS window, `ingest --force` won't see it and it'll be lost from the collection. Check `pep-oracle episodes | head -60` before Step 4 — if the earliest-ingested episode number is older than the earliest-in-feed episode number, abort and migrate via a one-off script that iterates chunks from the backup and re-embeds instead.
