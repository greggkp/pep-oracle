# Modal-based cloud diarization

## Problem

Speaker diarization via pyannote on the current hardware (OptiPlex 7040 Micro — 4-core i5-6500T, no GPU) takes hours per 2-hour episode. The chunked-diarization workaround caps RAM but not CPU time. To hit "a few minutes per episode" we need GPU acceleration; the Micro has no PCIe slot so offloading to a cloud GPU is the only path.

## Goal

Ingest a 2-hour episode end-to-end in ~5-7 minutes by running pyannote 3.1 on a Modal GPU instead of the local CPU. Keep all other pipeline stages (Whisper transcription via OpenAI API, chunking, embedding, ChromaDB upsert) unchanged.

## Non-goals

- Local fallback if Modal is unavailable. The systemd ingest timer retries on its next tick; manual retry is acceptable for interactive ingestion.
- Supporting multiple cloud backends behind an abstraction. Modal only.
- Moving Whisper to the cloud too (it's already an OpenAI API call).

## Architecture

```
Local (OptiPlex Micro)                Modal Cloud (L4 GPU)
──────────────────────                ────────────────────
ingest pipeline                       @app.function(gpu="L4")
  ↓                                   def diarize(audio_url,
diarize_cloud call                      num_speakers):
  └─ lookup deployed function            download audio_url
  └─ call .remote(url, n_speakers)       run pyannote 3.1
  └─ receive list[SpeakerSegment] ←──    return [{start,end,speaker}]
  ↓
align_speakers / map_names (unchanged)
  ↓
chunk / embed / store (unchanged)
```

### Audio transfer

The podcast RSS enclosure URL is passed to Modal. Modal downloads directly from the podcast host. No upload from the Micro.

### Error behavior

- Modal SDK errors (deploy missing, auth fail, GPU unavailable, pyannote error) propagate as exceptions. Caught by `ingest_all`'s per-episode `try/except`, counted as `failed: 1`, error string visible via server log (`logger.info("ingest: %s")`).
- Audio URL 404/timeout inside Modal raises `RuntimeError("audio fetch failed: ...")` — same path.
- Modal function has hard `timeout=1800` (30 min) to catch stuck pipelines.
- No automatic local fallback. Systemd timer re-runs on schedule; failed episodes retry on next tick.

## Components

### 1. `cloud/diarize_modal.py` (new) — Modal function definition

- Docker image: `python:3.12-slim` + `pyannote.audio==3.3.*` + `ffmpeg` + `numpy`
- GPU: `L4` (24 GB VRAM, ~$0.80/hr). Adequate for 2-hour audio in one pass, no chunking needed.
- Function: `diarize(audio_url: str, num_speakers: int | None) -> list[dict]`
  - Downloads audio via `urllib` to a temp file
  - Loads pyannote pipeline (cached across warm invocations)
  - Runs pipeline on the full audio (no chunking — GPU VRAM handles it)
  - Returns `[{"start": float, "end": float, "speaker": str}, ...]`
- HF_TOKEN accessed via Modal Secret `huggingface-token`
- Deployed manually via `modal deploy cloud/diarize_modal.py`; re-deploy only when this file changes

### 2. `src/pep_oracle/transcripts/diarize.py` (modified)

Replace `diarize_audio`'s body:

```python
def diarize_audio(
    audio_url: str,
    num_speakers: int | None = None,
) -> list[SpeakerSegment]:
    f = modal.Function.from_name("pep-oracle-diarize", "diarize")
    raw = f.remote(audio_url, num_speakers)
    return [SpeakerSegment(**r) for r in raw]
```

**Signature change**: `audio_path: Path` → `audio_url: str`. All callers update.

Delete:
- `_diarize_chunked`
- `_audio_duration_seconds`
- `_load_pipeline`
- `_run_pipeline`
- `CHUNK_SECONDS`, `CHUNK_OVERLAP_SECONDS`

Keep: `SpeakerSegment` dataclass, `align_speakers`, `map_speaker_names`, `_trim_to_speaker`, `load_speaker_profiles`, cache helpers.

Modify (not delete) `diarize_cached`: signature changes from `audio_path: Path` to `audio_url: str`. Caching logic unchanged (cache key is still `episode_guid`).

### 3. Callers (modified)

- `diarize.py` `diarize_cached` (line ~442): pass `episode.audio_url` instead of `audio_path`. Signature updates to accept `audio_url` instead of `audio_path`. Propagate through its callers.
- `cli.py` (line ~124): pass `episode.audio_url` instead of the local audio path.
- `ingest.py` `_ingest_one`: update call site to pass the URL.

### 4. Tests (modified)

- Delete: chunking tests (`_diarize_chunked`, label-stitching, `_audio_duration_seconds`). Roughly 5-6 tests.
- Add: `test_diarize_audio_calls_modal` — mocks `modal.Function.from_name` to return a fake function whose `.remote()` returns canned segment dicts. Verifies deserialization to `SpeakerSegment` list.
- Keep unchanged: alignment tests, `_trim_to_speaker` tests, speaker-mapping tests.

### 5. Dependencies (modified)

- Add `modal>=0.64` to main deps in `pyproject.toml`.
- Remove the `[diarize]` extra (pyannote-audio, torch). This drops ~2 GB of unused local deps.
- Update `CLAUDE.md` to reflect the new diarization architecture.

### 6. Environment

New required env vars (add to `.env.example` and document):
- `MODAL_TOKEN_ID` — Modal authentication (from `modal token new`)
- `MODAL_TOKEN_SECRET` — Modal authentication

`HF_TOKEN` moves from local `.env` to Modal Secret `huggingface-token`. The local `.env` no longer needs it.

## Deployment workflow

One-time setup:
1. `pip install modal` in a dev shell
2. `modal token new` — authenticates
3. `modal secret create huggingface-token HF_TOKEN=<token>`
4. `modal deploy cloud/diarize_modal.py`
5. Add `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` to `/opt/pep-oracle/app/.env`
6. Restart `pep-oracle-api.service`

Subsequent updates:
- `modal deploy cloud/diarize_modal.py` when the cloud function changes
- No redeploy needed for changes to the client code

## Testing strategy

- Unit: mock `modal.Function.from_name` in `test_diarize.py`. No network calls.
- Integration: defer. A `@pytest.mark.live` test that hits real Modal could be added later if needed.
- Manual verification after deploy: trigger one episode via the web UI, confirm completion in ~5-7 min, compare diarization quality against an existing locally-diarized episode.

## Expected behavior

| Metric | Current | After |
|---|---|---|
| Per-episode time (2h audio) | ~1-2 hr | ~5-7 min |
| Peak RSS (Micro) | ~2.7 GB | ~500 MB (no pyannote/torch loaded) |
| Monthly cost (4 episodes/week) | $0 | ~$1 |
| Local deps size | ~2.5 GB (torch+pyannote) | ~50 MB (modal SDK) |

## Open risks

- **Modal cold start**: first invocation after ~10 min idle adds ~15-30s container boot. Acceptable; episode ingestion is infrequent.
- **pyannote version drift**: the `DiarizeOutput` vs `Annotation` issue (fixed in `3bc81b8`) was caused by pyannote version bump. Pin `pyannote.audio==3.3.*` in the Modal image to avoid surprises.
- **Modal SDK breaking changes**: pin `modal>=0.64,<1` initially.
- **Audio URL rot**: if a podcast CDN ever changes URLs, diarization fails for those episodes. Cached diarization results in `~/.pep-oracle/cache/diarization/` are preserved, so already-processed episodes are unaffected.
