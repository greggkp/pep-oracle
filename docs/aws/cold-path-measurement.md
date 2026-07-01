# Cold-path measurement (MCP latency)

Goal: find where a **cold** `search_us_politics_commentary` request spends its
time before deciding which cold-start optimization to invest in. This doc covers
the instrumentation added for that and how to read it.

## The two costs on a cold start

1. **INIT phase** — container pull + Python runtime + module imports +
   `mount_mcp_if_configured(app)` (runs at import). Does **not** touch the
   corpus. Reported by Lambda itself as `Init Duration` (see below) — no app
   instrumentation needed.
2. **First-request init** — the corpus loads **lazily on the first `/mcp`
   search**, so the first request also pays: S3 download of the parquet, parquet
   parse (incl. the arrow→numpy embedding load), a second S3 round
   trip for manifest validation, the BM25 index build, and the first Bedrock
   `InvokeModel` (incl. boto3 client construction). This is the part the timing
   logs below break down.

## Instrumentation

`pep_oracle.timing.timed(phase, **fields)` logs one structured line per phase to
the `pep_oracle.timing` logger:

```
INFO:pep_oracle.timing: timing phase=<name> ms=<n.n> [key=value ...]
```

Phases emitted on the search path (a cold request hits all of them; a warm one
only `search.*`, since corpus/BM25 are cached):

| phase | where | cold-only? | notes |
|-------|-------|-----------|-------|
| `search.total` | `mcp_server.search_pep` | no | end-to-end (minus response formatting) |
| `search.embed` | `mcp_server.search_pep` | no | Bedrock embed round-trip |
| `embed.client_init` | `embeddings._bedrock_client` | yes | boto3 import + client construction |
| `search.corpus_fetch` | `mcp_server.search_pep` | no | cached on warm; triggers the loads below on cold/refresh |
| `corpus.load_and_validate` | `corpus.current_corpus` | cold/refresh | download + parse + manifest validate |
| `corpus.download` | `corpus.load_current` | cold/refresh | S3 GET of the parquet |
| `corpus.parse` | `corpus.load_current` | cold/refresh | `bytes=` = parquet size |
| `corpus.parse_read_table` | `corpus.from_parquet_bytes` | cold/refresh | parquet decode (zstd decompress + arrow buffers) |
| `corpus.parse_columns` | `corpus.from_parquet_bytes` | cold/refresh | ids + docs `to_pylist` |
| `corpus.parse_embeddings` | `corpus.from_parquet_bytes` | cold/refresh **fallback only** | arrow→numpy float32 matrix load from the parquet column; `chunks=` = row count. Fires ONLY when no usable prebuilt embeddings sidecar loaded — like `hybrid.bm25_build`, its appearance on a non-empty corpus is an **ops signal** |
| `corpus.emb_download` | `corpus._load_prebuilt_embeddings` | cold/refresh | S3 GET of the embedding-matrix sidecar `vNNNN.emb.zst` |
| `corpus.emb_decode` | `corpus._load_prebuilt_embeddings` | cold/refresh | zstd decompress + `np.frombuffer` of the prebuilt float32 matrix; `bytes=` = frame size |
| `corpus.parse_metadata` | `corpus.from_parquet_bytes` | cold/refresh | metadata `to_pylist` + per-row `json.loads` |
| `corpus.index_download` | `corpus._load_prebuilt_index` | cold/refresh | S3 GET of the BM25 sidecar `vNNNN.bm25.zst` |
| `corpus.index_decode` | `corpus._load_prebuilt_index` | cold/refresh | zstd decompress + `json.loads` of the prebuilt index; `chunks=` = row count |
| `search.hybrid` | `mcp_server.search_pep` | no | candidate retrieval (cosine + BM25 + RRF) |
| `hybrid.bm25_build` | `hybrid._load_corpus` | cold/refresh **fallback only** | BM25 tokenize + idf/tf rebuild; `chunks=` = row count. Fires ONLY when no usable prebuilt index was loaded (pre-index artifact, or a rejected/stale index) — its appearance is an **ops signal** |
| `search.stats` | `mcp_server.search_pep` | no | `get_ingestion_stats` (full-corpus rescan per request) |

Note the `*.parse_embeddings`, `corpus.index_*`, `*.bm25_build`, and `corpus.*`
phases also fire on every **TTL refresh** (a request crossing
`CORPUS_REFRESH_TTL_SECONDS` onto a new corpus version), so they are not strictly
cold-start costs.

**Prebuilt BM25 index (2026-06-29).** The artifact now ships the BM25 index as an
immutable per-version sidecar (`corpus/vNNNN.bm25.zst`); the serving path decodes
it (`corpus.index_decode`, ~1s on Lambda) instead of rebuilding it
(`hybrid.bm25_build`, ~2.67s) — a ~1.7s cold-path win that also applies on TTL
refresh. The parquet is byte-unchanged, so `corpus.parse_read_table` does not
regress. On a happy-path cold request you should now see `corpus.index_download` +
`corpus.index_decode` and **no** `hybrid.bm25_build`. See
[prebuilt-bm25-index.md](prebuilt-bm25-index.md).

**Prebuilt embedding matrix (2026-07-01).** Same sidecar pattern, applied to the
embedding column: the artifact ships `corpus/vNNNN.emb.zst` (raw row-major float32
+ zstd, parquet-sha provenance in the frame). When it loads, `from_parquet_bytes`
reads only the `chunk_id`/`text`/`metadata` columns — parquet is columnar, so the
embedding column's pages (the bulk of the file's decode cost, the
`corpus.parse_read_table` ~1.37s prod phase) are never touched — and the matrix
comes from `np.frombuffer` on the decompressed sidecar (`corpus.emb_decode`).
Fallbacks stack: absent/stale/corrupt sidecar or a row-count mismatch at parse
time → decode the parquet column exactly as before (`corpus.parse_embeddings`
firing is the ops signal). The parquet stays byte-unchanged (rollback inert). On a
happy-path cold request you should now see `corpus.emb_download` +
`corpus.emb_decode`, a much smaller `corpus.parse_read_table`, and **no**
`corpus.parse_embeddings`.

## Reading it in CloudWatch

Lambda's own `REPORT` line gives the INIT-vs-request split:

```
REPORT RequestId: ...  Duration: <total> ms  Init Duration: <init> ms  ...
```

`Init Duration` is cost (1) above; it appears only on cold invocations.

A Logs Insights query to break down a cold request:

```
fields @timestamp, @message
| filter @message like /timing phase=/ or @message like /Init Duration/
| sort @timestamp asc
```

Or aggregate one phase across invocations:

```
fields @message
| parse @message "phase=* ms=*" as phase, ms
| filter ispresent(phase)
| stats avg(ms), max(ms), count() by phase
```

## Production measurement (2026-06-28)

First real read of CloudWatch after ~18 days live (serving Lambda
`PepOracleProdStack-ServeFnBA855C13`, 2048 MB, 30 s timeout). Window: last 30
days.

**Traffic shape.** 2001 Lambda invocations, but only **5 were actual
`/mcp` searches** (4 on 2026-06-10, 1 on 2026-06-26). The other ~1996 are
`/health`, `/version`, `/.well-known/...`, no-token `/mcp` 401s, and OAuth —
cheap (`avg @duration` across all invocations was 152 ms). 369 cold starts
(~18 %), `avg Init Duration` 2.38 s, `max` 9.4 s. `max @maxMemoryUsed` 1447 MB
of 2048 (memory is adequately sized).

**Every real search was cold.** All five searches paid the full corpus load +
BM25 build (each phase count = 5 = the `search.total` count). At this traffic
the container always scales to zero between queries, so **the cold path is the
only path** — there is effectively no warm search traffic. The warm-path
vectorized-cosine win (warm `search.hybrid` ~18 ms, local) is real but never
reaches a user here.

Cold `search.total` 4.4–7.0 s (avg 5.5 s). On a user's *first* query the ~2.4 s
INIT stacks on top → ~7–9 s wall clock. Breakdown (avg over the 5, corpus
6020→6139 chunks):

| phase | avg ms | note |
|-------|-------:|------|
| `search.total` | 5503 | end-to-end |
| `search.hybrid` | 2755 | dominated by ↓ |
| **`hybrid.bm25_build`** | **2670** | **biggest single phase** |
| `search.corpus_fetch` | 2594 | = load_and_validate |
| `corpus.parse` | 1907 | dominated by ↓ |
| **`corpus.parse_read_table`** | **1372** | **parquet zstd decode + arrow** |
| `corpus.download` | 406 | S3 GET of parquet |
| `search.embed` | 148 | Bedrock round-trip |
| `corpus.parse_metadata` | 126 | |
| `corpus.parse_columns` | 35 | |
| `embed.client_init` | 13 | |
| `corpus.parse_embeddings` | **8.5** | numpy load — **2026-06-10 fix confirmed in prod** |
| `search.stats` | 3 | |

**Conclusions vs. the original analysis.**
1. The 2026-06-10 embeddings-parse optimization holds in production:
   `corpus.parse_embeddings` is ~8 ms (was ~2.3 s). Done, verified.
2. The cold path is now dominated by two phases the doc hadn't quantified with
   prod data: **`hybrid.bm25_build` (~2.67 s)** and
   **`corpus.parse_read_table` (~1.37 s)** — together ~4 s of the ~5.5 s.
3. **Do _not_ warm the corpus/BM25 at INIT.** The corpus is lazy-loaded on first
   search, which is why 369 cold starts only cost ~152 ms each — the
   health-check/discovery cold starts (the overwhelming majority) never touch the
   corpus. Warming at INIT would push that ~4 s onto every cold start, not just
   the rare search.
4. Highest-leverage fix: ~~**ship a prebuilt BM25 index inside the corpus
   artifact** so a cold search skips the ~2.7 s build~~ — **done** (2026-06-29):
   the artifact ships a `vNNNN.bm25.zst` sidecar; cold search decodes it
   (`corpus.index_decode` ~1 s) instead of rebuilding (`hybrid.bm25_build`
   ~2.67 s). ~~Next target: `corpus.parse_read_table` (~1.37 s)~~ — **done**
   (2026-07-01): the `vNNNN.emb.zst` sidecar carries the embedding matrix, so
   the parse skips the embedding column (see above). Re-measure in prod; the
   expected next targets are `corpus.index_decode` (~1 s of JSON decode — a
   binary BM25 encoding would cut it) and the ~2.4 s Lambda INIT, which now
   bounds the floor (a scheduled warmer that exercises the search path is the
   only way a user gets a warm search at this traffic level).

## Decision

Once the data is in, compare the dominant phase against the candidate fixes:

- `Init Duration` dominates → trim imports / shrink the image, or fork to a
  `.zip` package so **SnapStart** (container images are unsupported) can snapshot
  the warmed runtime, or **provisioned concurrency** (continuous cost; account
  concurrency limit is 10).
- `corpus.parse_embeddings` / `corpus.parse` dominate → ~~load embeddings as a
  numpy `float32` matrix instead of `to_pylist()` (near-zero-copy from arrow);
  this also unlocks warm-path cosine vectorization~~ — **done** (2026-06-10):
  parse dropped ~2.3s→~0.1s and warm `search.hybrid` ~390ms→~18ms (6,020 chunks,
  local measurement).
- `hybrid.bm25_build` dominates → ~~ship a prebuilt index in the artifact, or
  build it lazily off the request path~~ — **done** (2026-06-29): prebuilt index
  shipped as the `vNNNN.bm25.zst` sidecar (`corpus.index_decode` replaces the
  build on the happy path). `hybrid.bm25_build` now firing at all means the
  prebuilt index was missing or rejected — investigate.
- `corpus.*` / `bm25_build` show up on otherwise-warm requests → that's the TTL
  refresh paying inline; consider refreshing in the background.
- Both heavy corpus phases dominate → also consider warming the corpus + BM25 in
  the INIT phase (module import, guarded fallback) so the first request is warm.
