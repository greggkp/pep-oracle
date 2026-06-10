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
| `corpus.parse_embeddings` | `corpus.from_parquet_bytes` | cold/refresh | arrow→numpy float32 matrix load; `chunks=` = row count |
| `search.hybrid` | `mcp_server.search_pep` | no | candidate retrieval (cosine + BM25 + RRF) |
| `hybrid.bm25_build` | `hybrid._load_corpus` | cold/refresh | BM25 tokenize + idf/tf; `chunks=` = row count |
| `search.stats` | `mcp_server.search_pep` | no | `get_ingestion_stats` (full-corpus rescan per request) |

Note the `*.parse_embeddings`, `*.bm25_build`, and `corpus.*` phases also fire on
every **TTL refresh** (a request crossing `CORPUS_REFRESH_TTL_SECONDS` onto a new
corpus version), so they are not strictly cold-start costs.

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
- `hybrid.bm25_build` dominates → ship a prebuilt index in the artifact, or
  build it lazily off the request path.
- `corpus.*` / `bm25_build` show up on otherwise-warm requests → that's the TTL
  refresh paying inline; consider refreshing in the background.
- Both heavy corpus phases dominate → also consider warming the corpus + BM25 in
  the INIT phase (module import, guarded fallback) so the first request is warm.
