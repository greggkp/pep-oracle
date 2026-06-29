# Prebuilt BM25 index sidecar

The cold MCP search path used to spend ~2.67 s on the serving Lambda rebuilding
the BM25 lexical index over every chunk (`hybrid.bm25_build`) — the single
biggest cold phase, and at this traffic **every** real search is cold (see
[cold-path-measurement.md](cold-path-measurement.md)). The corpus artifact now
ships the index prebuilt so the serving path decodes it (~1 s) instead of
rebuilding it: a ~1.7 s cold-path win that also applies on every TTL refresh.

## What ships

One immutable, per-version sidecar object next to the parquet + manifest:

```
<CORPUS_URI>/corpus/vNNNN.parquet         # unchanged
<CORPUS_URI>/corpus/vNNNN.manifest.json   # unchanged (schema_ver still 1)
<CORPUS_URI>/corpus/vNNNN.bm25.zst        # NEW: prebuilt BM25 index
<CORPUS_URI>/corpus/current.json          # unchanged
```

The parquet, manifest, and `current.json` are **byte-unchanged**, so the
protected `corpus.parse_read_table` phase does not regress and a rollback to old
serving code is completely inert (it never looks for the new key). Embedding the
index in the parquet was measured and rejected: a 6.1 MB blob in parquet
key-value metadata bloated the file and ~6×'d `pq.read_table` (parquet re-parses
the whole footer on every read).

### Frame format (`vNNNN.bm25.zst`)

```
b"PEPBM25\x00"            # 8-byte magic — reject a wrong/garbage object pre-decompress
uint64 little-endian      # uncompressed length (pa.decompress needs it for zstd)
zstd(json_bytes)          # zlib/pyarrow zstd of json.dumps(state)
```

`state` is `BM25.to_dict()` (`k1, b, N, avgdl, idf, tf, doclen, index_format,
code_fingerprint`) plus `parquet_sha256` and `chunk_count` for provenance. JSON
keeps it dependency-free and round-trips IEEE doubles exactly; zstd is already
available via pyarrow (`pa.compress` / `pa.decompress(..., asbytes=True)`).
`tf` deserializes to plain `dict`s and `scores()` reads them via `.get`, so a
reconstructed instance scores **bit-identically** to a fresh build (verified
max abs diff 0.0 over the real corpus).

## Write path

`corpus.write_artifact` builds the index with the canonical `lexical.build_bm25`
and writes the sidecar under the immutable version key **after** the parquet +
manifest but **before** the `current.json` flip (the flip stays last, so a reader
still sees old-or-new, never half). A serialize/upload failure only logs a
warning and the publish proceeds — the index is a pure latency optimization and
serving falls back to rebuilding. The daily Fargate ingest needs no change (it
already calls `write_artifact`).

## Load path

`corpus.load_current` downloads + sha-verifies the parquet (as before), then
`_load_prebuilt_index` GETs the sidecar and validates it before attaching it to
the `InMemoryCorpus` as `prebuilt_bm25`. `hybrid._load_corpus` adopts that index
when present (and `N == count`), else rebuilds under `hybrid.bm25_build`.

## Why a stale index can never silently mis-score

Validation is layered; **any** doubt → return `None` → rebuild (correctness over
latency):

1. **Magic** rejects a wrong object before decompressing.
2. **zstd + json.loads** raise on truncation/corruption (caught → rebuild).
3. **Provenance coupling** — `parquet_sha256` in the payload must equal the
   already-verified parquet sha (`current.json`'s `sha256`). This cryptographically
   proves the index was built from *this exact parquet's* docs and row order
   (which `scores()` alignment depends on), catching a stale index left by a
   previous publish — something a chunk-count check alone cannot (two corpora can
   share a count).
4. **`code_fingerprint`** — a sha of the `tokenize` / `normalize_numbers` /
   `BM25.scores` source plus the `_TOKEN` regex and `_NUM_WORDS` table, computed
   once at import. If the serving code's preprocessing/scoring differs from the
   code that built the index (e.g. serving deployed ahead of the ingest that built
   it), the fingerprints differ → reject → rebuild with the live code.
5. **Size guards** — `chunk_count` / `N` / `len(tf)` / `len(doclen)` must agree;
   `hybrid` re-checks `N == count` as defense-in-depth.

### Deploy / rollback matrix

| case | behavior |
|------|----------|
| new code + new artifact (index matches) | decode + adopt; no `hybrid.bm25_build` |
| new code + OLD artifact (no sidecar) | GET 404 → `None` → rebuild (as today) |
| OLD code (rollback) + new artifact | old code ignores the sidecar; serving byte-identical to before |
| new code + stale/corrupt/mismatched index | every sub-case fails a check above → rebuild |

## Ops notes

- New timing phases: `corpus.index_download`, `corpus.index_decode`. On a happy
  cold request you should see these and **no** `hybrid.bm25_build`; `bm25_build`
  reappearing is the signal that the index was missing or rejected.
- The index only exists once an **index-bearing ingest** republishes. Until then
  (and for already-published `v0001..vNNNN`) serving rebuilds — no regression.
- After the first index-bearing ingest, run `uv run pytest -m live` to confirm the
  round-trip on the real corpus and that `recall@k` / `MRR` are unchanged (scores
  are byte-identical, so they should be).
- Cost: +~6 MB immutable per corpus version in S3, and +1 GET on each cold/refresh
  search. Negligible at this traffic.
- A cosmetic edit / `ruff format` of `lexical.py`'s tokenizer/scorer changes the
  `code_fingerprint`, so deployed indexes are rejected (safe rebuild) until the
  next ingest republishes. Acceptable graceful degradation; the alternative
  (manual-bump-only) risks a *persistent silent* wrong-scores bug.
