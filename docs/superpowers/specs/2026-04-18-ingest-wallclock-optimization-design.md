# Ingest Wall-Clock Optimization (A1)

**Status:** Draft
**Date:** 2026-04-18
**Scope:** Reduce per-episode ingest wall-clock (ingest-start → queryable) without regressing transcript or diarization output quality.

## Goal

Cut ingest wall-clock for a typical 2-hour episode from roughly 12 minutes to roughly 3–4 minutes by making three coordinated changes to the Modal-side pipeline and its orchestrator.

The metric optimized here is **ingest-start → queryable**. Trigger latency (systemd timer interval, RSS poll cadence) is explicitly out of scope.

## Non-goals

- Fan-out transcription (splitting audio into parallel Modal calls).
- Modal `keep_warm` or similar cold-start reduction.
- Any query-side optimization.
- Changes to the RSS poll cadence or systemd timer interval.
- Changes to chunking, embeddings, or ChromaDB layout.
- Moving diarization off the critical path (decision: diarization must be complete before an episode is considered queryable).

## Current pipeline (sequential, ~12 min)

```
RSS fetch
  → Modal transcribe (L4, large-v3)           [5–10 min]
  → Modal diarize (L4, pyannote)              [~5 min]
  → align speakers to transcript segments     [<1s]
  → chunk by time window                      [<1s]
  → fastembed embed chunks (CPU)              [10–60s]
  → ChromaDB upsert                           [<1s]
```

Transcription and diarization are both Modal functions that take only the audio URL from the RSS enclosure. They are independent — neither consumes the other's output — but today they run sequentially.

## Target pipeline (~3–4 min)

```
RSS fetch
  → concurrent:
      ├─ Modal transcribe (A100, large-v3-turbo)  [~1 min]
      └─ Modal diarize (A100, pyannote)           [~2–3 min]  ← new critical path
  → align speakers to transcript segments         [<1s]
  → chunk by time window                          [<1s]
  → fastembed embed chunks (CPU)                  [10–60s]
  → ChromaDB upsert                               [<1s]
```

Diarization becomes the new bottleneck once transcription is sped up; upgrading its GPU is where the remaining wall-clock win comes from.

## Design

Three coordinated changes:

### 1. Concurrent Modal orchestration in `ingest.py`

`ingest.py` currently calls `transcripts/manager.py` (Whisper via `cloud/transcribe_modal.py`) and then `transcripts/diarize.py` (pyannote via `cloud/diarize_modal.py`) sequentially. Replace this with a concurrent submission using `concurrent.futures.ThreadPoolExecutor`, since Modal's `.remote()` is a blocking synchronous call:

```python
with ThreadPoolExecutor(max_workers=2) as pool:
    t_future = pool.submit(manager.get_transcript, episode, ...)
    d_future = pool.submit(diarize.get_diarization, episode, ...) if diarize_enabled else None
    transcript = t_future.result()
    diarization = d_future.result() if d_future else None
```

Both helpers already check their disk cache before spawning Modal work, so re-ingests of already-processed episodes short-circuit with zero overhead from the parallel wrapper.

If diarization is disabled (CLI `--no-diarize`), the second submission is skipped — behavior identical to today.

### 2. Whisper model swap

`cloud/transcribe_modal.py`: change `faster-whisper large-v3` → `large-v3-turbo`. Same CTranslate2 runtime, different model name. The `pep-oracle-whisper-cache` Modal volume will seed the new weights on first deploy.

Transcript output schema is unchanged, so `transcripts/whisper.py` and the disk cache format at `~/.pep-oracle/cache/transcripts/{guid}.whisper.json` remain compatible.

### 3. GPU tier upgrade

Both Modal functions go from `gpu="L4"` to `gpu="A100"` (H100 is an acceptable substitute if pricing/availability favor it at deploy time — architecturally equivalent).

- Transcribe on A100 + turbo model: ~1 min end-to-end.
- Diarize on A100: ~2–3 min, becomes the new critical path.

## Components changed

| File | Change |
|------|--------|
| `cloud/transcribe_modal.py` | `large-v3` → `large-v3-turbo`; `gpu="L4"` → `gpu="A100"` |
| `cloud/diarize_modal.py` | `gpu="L4"` → `gpu="A100"` |
| `src/pep_oracle/ingest.py` | Run transcribe + diarize concurrently via `ThreadPoolExecutor`; add per-stage duration logging |
| `CLAUDE.md` | Update cost/wall-clock/model references |

No changes to `server.py`, `cli.py`, `deploy/pep-oracle-ingest.service`, chunking, embeddings, ChromaDB schema, or the cache file formats.

## Data flow

See the "Current" and "Target" pipelines above. The only structural change is that transcribe and diarize run concurrently instead of sequentially; everything downstream is unchanged.

**Cache-hit re-ingest:** both helpers hit their local cache before making a Modal call, so the parallel wrapper is effectively instantaneous for already-processed episodes.

## Error handling

- Modal failures in either function propagate unchanged (current fail-fast behavior is preserved).
- No new timeout logic; Modal's per-call limits remain authoritative.
- When one future raises, the other is allowed to finish or is discarded on `ThreadPoolExecutor` context exit. Modal `.remote()` calls cannot be cleanly cancelled mid-flight, and a completed diarize/transcript cache file is always useful for the next run.
- The two helpers write to distinct cache paths (`cache/transcripts/{guid}.whisper.json` vs `cache/diarization/{guid}.json`), so there is no shared-state contention when they run concurrently.

## Testing

- **Unit test for the concurrent helper:** mock both Modal calls with artificial delays and assert the wrapper's wall-clock is approximately `max(t1, t2)`, not `t1 + t2`. Add a case where one mocked call raises and confirm the exception propagates.
- **Existing Modal mocking** (`Function.from_name(...).remote(...)` monkeypatch) continues to work — no new test infrastructure needed.
- **No integration tests against real Modal.** Mocked behavior is sufficient for correctness. Speed and turbo quality are validated manually during rollout.

## Turbo quality validation

Before cutting over to `large-v3-turbo`, run both models on 3 recent episodes and compare transcripts. Accept turbo if:

- No systematic drops of speaker names, show regulars, or domain-specific proper nouns.
- No missing segments or large chunks of dropped audio.
- Subjective reading of 2–3 random 5-minute windows per episode reveals no obvious quality cliff.

Comparison artifacts are kept locally under `docs/superpowers/specs/artifacts/` (not committed) for reference.

If turbo fails this bar, fall back to `large-v3` on A100. That still delivers a material wall-clock improvement from the GPU upgrade and the concurrent orchestration alone.

## Observability

Add per-stage INFO logging in `ingest.py`:
- `transcribe_elapsed`
- `diarize_elapsed`
- `embed_elapsed`
- `total_elapsed`

This makes future regressions easy to localize to a specific stage.

## Rollout plan

1. Upgrade both Modal functions' GPU tier. `modal deploy`. Run one ingest; confirm success and measure wall-clock.
2. Swap Whisper to `large-v3-turbo`. `modal deploy`. Run the 3-episode quality comparison. Accept or roll back.
3. Land the concurrent orchestration change in `ingest.py`. Run one more end-to-end ingest; record final wall-clock.
4. Update `CLAUDE.md` with the new cost, wall-clock, and model references.

## Success criteria

- End-to-end ingest of a 2-hour episode completes in ≤ 5 minutes (target: 3–4 min) on a cache-miss run.
- Cache-hit re-ingest wall-clock is unchanged from today.
- Transcript quality on a 3-episode comparison is judged acceptable (criteria above) — or the turbo change is reverted and we accept the smaller speedup.
- No regressions in diarization output or downstream chunking/embedding/retrieval behavior.
