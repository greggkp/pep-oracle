# Local fastembed embeddings — design

## Goal

Drop the OpenAI dependency by moving embedding generation from the OpenAI API
(`text-embedding-3-small`) to a locally-hosted model via `fastembed`. After this
change, the only external services pep-oracle talks to are Modal (transcription,
diarization) and Anthropic (query-time LLM calls). No embedding API keys, no
round trips, no rate limits for embeddings.

## Non-goals

- Not a cost optimization — embeddings are pennies/month today. The motivator is
  removing a dependency, not saving money.
- Not an embedding-quality upgrade per se — although `bge-large-en-v1.5` is
  slightly ahead of `text-embedding-3-small` on MTEB, we're not benchmarking or
  validating quality in this change.
- Not a change to the retrieval pipeline (chunking, top-k, reranking, recency
  boost, speaker filtering) — those all stay as-is.

## Choices (with rationale)

| Decision | Choice | Why |
|---|---|---|
| Model | `BAAI/bge-large-en-v1.5` (1024-dim, 335M params, ≈1.3 GB on disk, ≈1.5 GB RAM resident) | User has an always-on OptiPlex with ample RAM. MTEB ~65 vs ~62-63 for `text-embedding-3-small`. Long-tail retrieval (names, dates) matters for transcript Q&A. |
| Library | `fastembed` | ONNX-based, no torch dep, ~150 MB install, ships pre-converted BGE weights, one-line embed call. |
| Load strategy | Lazy singleton in `embeddings.py` | Server pays ~5s once at first-query; subsequent queries hit warm model. CLI ingests pay ~5s per invocation, amortized over hundreds of chunks. |
| Migration | Export → drop collection → `ingest --force` | Dim change (1536 → 1024) requires a fresh Chroma collection. Cached transcripts + diarization make the rebuild free (no Modal calls). |

## Components touched

- `src/pep_oracle/embeddings.py` — rewrite in place.
- `src/pep_oracle/query.py` — drop `openai_client` parameter from `answer_question`.
- `src/pep_oracle/cli.py`, `src/pep_oracle/server.py` — remove `openai_client` call-sites.
- `src/pep_oracle/config.py` — remove `OPENAI_API_KEY` validation if present.
- `tests/test_embeddings.py` — replace batching/retry tests with one integration
  test asserting output shape (1024-dim, non-zero, correct length).
- `pyproject.toml` — add `fastembed>=0.4`, remove `openai`.
- `CLAUDE.md` — update the "Embedding batches of 20" bullet and the required-env list.

## Architecture

```
embeddings.py (new shape)
  _model: TextEmbedding | None = None
  def _get_model() -> TextEmbedding:
      global _model
      if _model is None:
          _model = TextEmbedding("BAAI/bge-large-en-v1.5")
      return _model

  def embed_texts(texts: list[str]) -> list[list[float]]:
      return [v.tolist() for v in _get_model().embed(texts)]
```

No retry logic, no batch-of-20 (fastembed batches internally), no `client`
parameter. Callers (`ingest.py`, `query.py`) drop their `client` plumbing.

## Error handling

None added. Fastembed's model download-on-first-use either succeeds or raises
with a clear error. No fallback to OpenAI, no retry, no silent degradation.
Matches the fail-fast posture of the Modal transcription and diarization paths.

## Testing

- **Existing ingest/query tests**: unchanged. They mock `embed_texts` directly
  via `patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)` — that
  pattern keeps working.
- **`test_embeddings.py`**: delete the current batching/retry tests. Add one
  integration test that loads bge-large, calls `embed_texts(["hello", "world"])`,
  and asserts `len==2`, `len(result[0])==1024`, and vectors are non-zero and
  non-identical. Slow first run (~10-20s due to model download + load), fast
  thereafter. Acceptable.

## Migration plan (runbook)

1. **Backup**: `pep-oracle export ~/pep-backup-pre-fastembed-$(date +%F).json`
2. **Drop collection**: in a quick python one-liner or a small CLI step —
   `from pep_oracle.store import get_client, get_collection; get_client().delete_collection("pep_oracle")`.
3. **Rebuild**: `pep-oracle ingest --force`. All transcripts are cached at
   `~/.pep-oracle/cache/transcripts/*.whisper.json`; all diarizations are cached
   at `~/.pep-oracle/cache/diarization/*.json`. No Modal calls fire. Wall-clock:
   rough estimate 5–15 min for 48 episodes on bge-large CPU.
4. **Verify**: `pep-oracle status` shows 48 episodes and a chunk count similar
   to the pre-migration figure. Run two or three sanity queries that worked well
   before and confirm they still do.

If anything is off: restore from the JSON backup via `pep-oracle import`.

## Risks and flags

- **Feed rollover**: if any of our 48 ingested episodes has rolled out of the
  100-episode RSS window, `ingest --force` won't re-ingest it. Current state
  shows earliest-ingested = Ep 169 and earliest-in-feed also = Ep 169, so we're
  safe today. If the gap ever opens up in the future, do the migration then via
  a one-off script that iterates chunks from the backup and re-embeds.
- **bge-large download**: ~1.3 GB on first use. One-time, goes into
  `~/.cache/fastembed` (or whatever fastembed defaults to). Not a blocker but
  worth mentioning for CI hosts with small disks.
- **CPU ingest latency**: bge-large on CPU will embed ~50-100 chunks/sec on a
  modern desktop. For a typical 300-chunk episode, single-episode ingest after
  this change adds a few seconds of embed time — imperceptible vs the 5–10 min
  Modal transcription stage.
