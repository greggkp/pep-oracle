# Ingest Wall-Clock Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut ingest wall-clock of a 2-hour episode from ~12 min to ~3–4 min by running Modal transcribe + diarize concurrently, upgrading both GPUs to A100, and swapping Whisper to `large-v3-turbo`.

**Architecture:** Refactor `diarize.py` to expose a Modal-only entry point (`get_speaker_segments`) so it can run in a `ThreadPoolExecutor` alongside `get_transcript`. Alignment/name-mapping runs after both futures resolve. Modal config changes (GPU tier + model name) are independent commits so they can be rolled back individually.

**Tech Stack:** Python 3.12, Modal (GPU jobs), faster-whisper / CTranslate2 (`large-v3-turbo`), pyannote 3.1 (diarization), `concurrent.futures.ThreadPoolExecutor`, pytest.

**Spec:** `docs/superpowers/specs/2026-04-18-ingest-wallclock-optimization-design.md`

---

## File Structure

| File | Role | Change type |
|------|------|-------------|
| `src/pep_oracle/transcripts/diarize.py` | Diarization helpers (Modal call + alignment + naming) | Refactor: split `diarize_transcript` into `get_speaker_segments` (parallel-safe) + `apply_diarization` (sync post-proc). Keep `diarize_transcript` as a thin wrapper for back-compat. |
| `src/pep_oracle/ingest.py` | Ingest orchestrator | Add `_run_transcribe_and_diarize` helper using `ThreadPoolExecutor`; call `apply_diarization` after both futures resolve; add per-stage timing logs. |
| `tests/test_ingest.py` | Ingest tests | Add parallelism test (mocked Modal calls with sleeps; assert wall-clock ≈ max). |
| `tests/test_diarize.py` | Diarize tests | Add a test for the new `get_speaker_segments` cache-hit path. |
| `cloud/transcribe_modal.py` | Modal Whisper function | `gpu="L4"` → `gpu="A100"`; `"large-v3"` → `"large-v3-turbo"`. |
| `cloud/diarize_modal.py` | Modal pyannote function | `gpu="L4"` → `gpu="A100"`. |
| `CLAUDE.md` | Project docs | Update cost, wall-clock, and Whisper model references. |

---

## Task 1: Extract `get_speaker_segments` and `apply_diarization` from `diarize_transcript`

**Rationale:** `diarize_transcript` today bundles the Modal call, alignment, and name-mapping into one function. Only the Modal call (`diarize_audio` + cache) is safe to run concurrently with `get_transcript`. We split it so the orchestrator can parallelize just that part.

**Files:**
- Modify: `src/pep_oracle/transcripts/diarize.py` (the `diarize_transcript` function at lines 180-220)
- Test: `tests/test_diarize.py`

- [ ] **Step 1: Write a failing test for the new `get_speaker_segments` function**

Append to `tests/test_diarize.py`:

```python
def test_get_speaker_segments_uses_cache(tmp_path, monkeypatch):
    """If a diarization cache file exists, get_speaker_segments returns it without calling Modal."""
    from pep_oracle.transcripts.diarize import (
        SpeakerSegment, get_speaker_segments, _save_cache,
    )
    from pep_oracle import config

    monkeypatch.setattr(config, "DIARIZATION_CACHE_DIR", tmp_path)
    # Also patch the name imported into diarize.py at import time
    from pep_oracle.transcripts import diarize as diarize_mod
    monkeypatch.setattr(diarize_mod, "DIARIZATION_CACHE_DIR", tmp_path)

    cached = [SpeakerSegment(speaker="S1", start=0.0, end=10.0)]
    _save_cache(cached, tmp_path / "guid-x.json")

    def _boom(*a, **k):
        raise AssertionError("diarize_audio should not be called on cache hit")

    monkeypatch.setattr(diarize_mod, "diarize_audio", _boom)

    result = get_speaker_segments(audio_url="https://x", episode_guid="guid-x")
    assert len(result) == 1
    assert result[0].speaker == "S1"
```

- [ ] **Step 2: Run the test — it should fail because `get_speaker_segments` does not exist yet**

Run: `uv run pytest tests/test_diarize.py::test_get_speaker_segments_uses_cache -v`
Expected: `ImportError` / `AttributeError` on `get_speaker_segments`.

- [ ] **Step 3: Add `get_speaker_segments` and `apply_diarization` to `src/pep_oracle/transcripts/diarize.py`**

Replace the body of `diarize_transcript` (lines 180-220) with a thin wrapper, and add two new functions above it. The final state of that section should be:

```python
def get_speaker_segments(
    audio_url: str,
    episode_guid: str,
    num_speakers: int | None = None,
    progress_callback=None,
) -> list[SpeakerSegment]:
    """Fetch speaker segments (from cache or via Modal).

    Safe to call concurrently with get_transcript — writes only to its own
    per-episode cache file.
    """
    ensure_dirs()
    cache_path = DIARIZATION_CACHE_DIR / f"{episode_guid}.json"
    if cache_path.exists():
        click.echo("  Diarization: cached")
        return _load_cached(cache_path)

    if progress_callback:
        progress_callback("diarizing speakers")
    click.echo("  Diarizing speakers...", nl=False)
    speaker_segments = diarize_audio(audio_url, num_speakers=num_speakers)
    _save_cache(speaker_segments, cache_path)
    unique = len(set(s.speaker for s in speaker_segments))
    click.echo(f" {unique} speakers, {len(speaker_segments)} segments")
    return speaker_segments


def apply_diarization(
    transcript_segments: list[TranscriptSegment],
    speaker_segments: list[SpeakerSegment],
    profile_path: Path | None = None,
) -> list[TranscriptSegment]:
    """Align transcript segments with speaker turns and map to real names.

    No Modal calls; operates on already-fetched data.
    """
    aligned = align_speakers(transcript_segments, speaker_segments)
    named = map_speaker_names(aligned, speaker_segments, profile_path)

    profiles = load_speaker_profiles(profile_path)
    if not profiles:
        click.echo("  Warning: No speaker profiles found. Using generic labels.")
        click.echo("  Run 'pep-oracle identify-speakers --episode <N>' to set up profiles.")
    return named


def diarize_transcript(
    transcript_segments: list[TranscriptSegment],
    audio_url: str,
    episode_guid: str,
    num_speakers: int | None = None,
    profile_path: Path | None = None,
    progress_callback=None,
) -> list[TranscriptSegment]:
    """Full diarization pipeline: fetch speaker segments, align, map names.

    Thin wrapper over get_speaker_segments + apply_diarization, kept for
    backward compatibility. New code should call the two halves separately
    so the Modal call can be parallelized with transcription.
    """
    speaker_segments = get_speaker_segments(
        audio_url=audio_url,
        episode_guid=episode_guid,
        num_speakers=num_speakers,
        progress_callback=progress_callback,
    )
    return apply_diarization(transcript_segments, speaker_segments, profile_path)
```

- [ ] **Step 4: Run the new test — it should pass**

Run: `uv run pytest tests/test_diarize.py::test_get_speaker_segments_uses_cache -v`
Expected: PASS.

- [ ] **Step 5: Run the full diarize + ingest test suites — no regressions**

Run: `uv run pytest tests/test_diarize.py tests/test_ingest.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pep_oracle/transcripts/diarize.py tests/test_diarize.py
git commit -m "refactor: split diarize_transcript into get_speaker_segments + apply_diarization"
```

---

## Task 2: Run transcribe + diarize concurrently in `ingest.py` with per-stage timing

**Files:**
- Modify: `src/pep_oracle/ingest.py` (`_ingest_one` function at lines 27-74)
- Test: `tests/test_ingest.py`

- [ ] **Step 1: Write a failing test for wall-clock parallelism**

Append to `tests/test_ingest.py`:

```python
import time

from pep_oracle.transcripts.diarize import SpeakerSegment


@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_runs_transcribe_and_diarize_concurrently(mock_embed, mock_fetch):
    """When diarize=True, transcribe and diarize should run concurrently,
    not sequentially. Wall-clock should be ~max(t1, t2), not t1 + t2."""
    collection = _fresh_collection()
    mock_fetch.return_value = [_make_episode(1)]

    slow_segments = FAKE_SEGMENTS
    slow_speakers = [SpeakerSegment(speaker="S1", start=0.0, end=20.0)]

    def slow_transcript(ep, progress_callback=None):
        time.sleep(0.3)
        return slow_segments, "whisper"

    def slow_speaker_segments(audio_url, episode_guid, num_speakers=None, progress_callback=None):
        time.sleep(0.3)
        return slow_speakers

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value=set()),
        patch("pep_oracle.ingest.get_transcript", side_effect=slow_transcript),
        patch(
            "pep_oracle.transcripts.diarize.get_speaker_segments",
            side_effect=slow_speaker_segments,
        ),
    ):
        start = time.monotonic()
        result = ingest_all(confirm_cost=False, diarize=True)
        elapsed = time.monotonic() - start

    assert result["processed"] == 1
    # Sequential would be ~0.6s; parallel should be ~0.3s. Allow 0.5s as the cap.
    assert elapsed < 0.5, f"expected parallel execution (<0.5s), got {elapsed:.2f}s"
```

- [ ] **Step 2: Run the test — it should fail on current sequential code**

Run: `uv run pytest tests/test_ingest.py::test_ingest_runs_transcribe_and_diarize_concurrently -v`
Expected: FAIL with the `assert elapsed < 0.5` assertion (elapsed ≈ 0.6s).

- [ ] **Step 3: Update imports at the top of `src/pep_oracle/ingest.py`**

Add these two imports (place them with the other stdlib/third-party imports at the top):

```python
import time
from concurrent.futures import ThreadPoolExecutor
```

- [ ] **Step 4: Add the concurrent helper above `_ingest_one` in `src/pep_oracle/ingest.py`**

Insert this new function immediately before `def _ingest_one(...)`:

```python
def _run_transcribe_and_diarize(
    episode: Episode,
    diarize_enabled: bool,
    progress_callback,
) -> tuple[list, str, list | None, float, float]:
    """Run Modal transcription and (optionally) Modal diarization concurrently.

    Returns (segments, source, speaker_segments_or_None, transcribe_elapsed, diarize_elapsed).
    If diarize_enabled is False, speaker_segments is None and diarize_elapsed is 0.0.
    """
    from pep_oracle.transcripts.diarize import get_speaker_segments

    def _time(fn, *a, **k):
        s = time.monotonic()
        r = fn(*a, **k)
        return r, time.monotonic() - s

    with ThreadPoolExecutor(max_workers=2) as pool:
        t_future = pool.submit(_time, get_transcript, episode, progress_callback=progress_callback)
        d_future = None
        if diarize_enabled:
            d_future = pool.submit(
                _time,
                get_speaker_segments,
                episode.audio_url,
                episode.guid,
                progress_callback=progress_callback,
            )

        (segments, source), t_elapsed = t_future.result()
        if d_future:
            speaker_segments, d_elapsed = d_future.result()
        else:
            speaker_segments, d_elapsed = None, 0.0

    return segments, source, speaker_segments, t_elapsed, d_elapsed
```

- [ ] **Step 5: Rewrite the transcription+diarization block in `_ingest_one`**

In `src/pep_oracle/ingest.py`, replace the current block (lines 34-47 — the `if progress_callback: progress_callback("transcribing") ...` down to the end of the `if diarize:` block) with:

```python
    if progress_callback:
        progress_callback("transcribing")

    segments, source, speaker_segments, t_elapsed, d_elapsed = _run_transcribe_and_diarize(
        episode, diarize, progress_callback,
    )
    logger.info(
        "transcribe_elapsed=%.1fs diarize_elapsed=%.1fs (concurrent, critical path=%.1fs)",
        t_elapsed, d_elapsed, max(t_elapsed, d_elapsed),
    )
    click.echo(f"  Transcript: {source} ({len(segments)} segments)")

    if diarize:
        from pep_oracle.transcripts.diarize import apply_diarization

        segments = apply_diarization(segments, speaker_segments, profile_path=None)
```

- [ ] **Step 6: Wrap the embed step with timing**

In `src/pep_oracle/ingest.py`, replace the existing embed lines:

```python
    if progress_callback:
        progress_callback(f"embedding {len(chunks)} excerpts")
    click.echo(f"  Embedding {len(chunks)} excerpts...", nl=False)
    embeddings = embed_texts([c.text for c in chunks])
    click.echo(" done")
```

with:

```python
    if progress_callback:
        progress_callback(f"embedding {len(chunks)} excerpts")
    click.echo(f"  Embedding {len(chunks)} excerpts...", nl=False)
    e_start = time.monotonic()
    embeddings = embed_texts([c.text for c in chunks])
    e_elapsed = time.monotonic() - e_start
    click.echo(" done")
    logger.info("embed_elapsed=%.1fs (chunks=%d)", e_elapsed, len(chunks))
```

- [ ] **Step 7: Run the parallelism test — it should pass now**

Run: `uv run pytest tests/test_ingest.py::test_ingest_runs_transcribe_and_diarize_concurrently -v`
Expected: PASS.

- [ ] **Step 8: Run the full ingest test suite — no regressions**

Run: `uv run pytest tests/test_ingest.py -v`
Expected: all PASS.

- [ ] **Step 9: Run the whole test suite as a final check**

Run: `uv run pytest -x -q`
Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add src/pep_oracle/ingest.py tests/test_ingest.py
git commit -m "feat: run Modal transcribe and diarize concurrently during ingest"
```

---

## Task 3: Upgrade both Modal functions' GPU tier to A100

**Rationale:** After Task 2, diarize (at ~5 min on L4) becomes the critical path once Whisper is fast. Upgrading both GPUs to A100 cuts their compute times roughly in half. These two file edits are grouped into one commit because they're symmetrical single-line changes.

**Files:**
- Modify: `cloud/transcribe_modal.py:34`
- Modify: `cloud/diarize_modal.py:32`

- [ ] **Step 1: Change the GPU tier in `cloud/transcribe_modal.py`**

Find:
```python
@app.function(
    image=image,
    gpu="L4",
    volumes={"/models": model_cache},
    timeout=1800,
)
```

Change to:
```python
@app.function(
    image=image,
    gpu="A100",
    volumes={"/models": model_cache},
    timeout=1800,
)
```

- [ ] **Step 2: Change the GPU tier in `cloud/diarize_modal.py`**

Find:
```python
@app.function(
    image=image,
    gpu="L4",
    secrets=[hf_secret],
    volumes={"/cache/hf": model_cache},
    timeout=1800,
)
```

Change to:
```python
@app.function(
    image=image,
    gpu="A100",
    secrets=[hf_secret],
    volumes={"/cache/hf": model_cache},
    timeout=1800,
)
```

- [ ] **Step 3: Run the test suite — Modal client code is mocked, so this should still pass**

Run: `uv run pytest -x -q`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add cloud/transcribe_modal.py cloud/diarize_modal.py
git commit -m "perf: upgrade Modal transcribe and diarize GPUs from L4 to A100"
```

- [ ] **Step 5: Deploy the Modal functions**

Run both commands (order doesn't matter):

```bash
modal deploy cloud/transcribe_modal.py
modal deploy cloud/diarize_modal.py
```

Expected: each prints a deployed-app URL.

- [ ] **Step 6: Smoke-test the deployment with one real ingest**

Pick a recent already-ingested episode and force-re-ingest it to exercise the new GPU tier end-to-end:

```bash
uv run pep-oracle ingest --episode 253 --force --diarize
```

Expected: command completes successfully. Check the logs for the timing lines added in Task 2:
```
transcribe_elapsed=<X>s diarize_elapsed=<Y>s (concurrent, critical path=<Z>s)
```

Record the numbers — they feed the success-criteria check at the end.

---

## Task 4: Swap Whisper to `large-v3-turbo` with quality validation

**Rationale:** `large-v3-turbo` is ~4–6× faster than `large-v3` on the same hardware at the cost of a small WER regression. We validate on 3 real episodes before accepting the swap. If quality fails, revert this commit and keep the A100 GPU win from Task 3.

**Files:**
- Modify: `cloud/transcribe_modal.py:67`

- [ ] **Step 1: Change the Whisper model name in `cloud/transcribe_modal.py`**

Find:
```python
        model = WhisperModel(
            "large-v3",
            device="cuda",
            compute_type="float16",
            download_root="/models",
        )
```

Change to:
```python
        model = WhisperModel(
            "large-v3-turbo",
            device="cuda",
            compute_type="float16",
            download_root="/models",
        )
```

- [ ] **Step 2: Run the test suite — Modal is mocked, should still pass**

Run: `uv run pytest -x -q`
Expected: all PASS.

- [ ] **Step 3: Commit (deploy before merging if quality is bad)**

```bash
git add cloud/transcribe_modal.py
git commit -m "perf: swap Whisper model to large-v3-turbo for ~4-6x transcribe speedup"
```

- [ ] **Step 4: Deploy the new Whisper function**

Run:
```bash
modal deploy cloud/transcribe_modal.py
```

Expected: deploy succeeds. First invocation will download `large-v3-turbo` weights into the `pep-oracle-whisper-cache` volume (adds ~30–60s to the first call only).

- [ ] **Step 5: Capture baseline transcripts using the old model on 3 episodes**

The existing cache files under `~/.pep-oracle/cache/transcripts/*.whisper.json` were written by prior `large-v3` runs — these are the baseline. Deploying the turbo model doesn't alter them (the turbo model only produces new transcripts on a cache-miss).

Pick three recent episodes (e.g., 250, 251, 252). For each, copy the existing transcript cache to an artifacts dir:

```bash
mkdir -p docs/superpowers/specs/artifacts
for ep in 250 251 252; do
  # look up the guid from RSS (or from ChromaDB) — adjust as needed
  guid=$(uv run python -c "
from pep_oracle.feed import fetch_episodes
for ep in fetch_episodes():
    if ep.episode_number == $ep:
        print(ep.guid); break
")
  cp "$HOME/.pep-oracle/cache/transcripts/${guid}.whisper.json" \
     "docs/superpowers/specs/artifacts/ep${ep}.large-v3.json"
done
```

Expected: 3 files written under `docs/superpowers/specs/artifacts/` (not committed — gitignored or simply never added).

- [ ] **Step 6: Re-ingest the same 3 episodes with the new model to produce turbo transcripts**

For each of the 3 episodes, delete the cached Whisper file and re-ingest (this forces a Modal call):

```bash
for ep in 250 251 252; do
  guid=$(uv run python -c "
from pep_oracle.feed import fetch_episodes
for ep in fetch_episodes():
    if ep.episode_number == $ep:
        print(ep.guid); break
")
  rm -f "$HOME/.pep-oracle/cache/transcripts/${guid}.whisper.json"
  uv run pep-oracle ingest --episode $ep --force --diarize
  cp "$HOME/.pep-oracle/cache/transcripts/${guid}.whisper.json" \
     "docs/superpowers/specs/artifacts/ep${ep}.turbo.json"
done
```

Expected: 3 fresh `.turbo.json` files, and the ingest log shows `transcribe_elapsed` dropping sharply (target ≤ 90s on A100).

- [ ] **Step 7: Diff and review the transcripts for quality**

For each episode, produce a unified diff of `.text` fields only (ignoring timestamp jitter):

```bash
for ep in 250 251 252; do
  uv run python -c "
import json
old = json.load(open('docs/superpowers/specs/artifacts/ep${ep}.large-v3.json'))
new = json.load(open('docs/superpowers/specs/artifacts/ep${ep}.turbo.json'))
old_text = '\n'.join(s['text'] for s in old)
new_text = '\n'.join(s['text'] for s in new)
open('docs/superpowers/specs/artifacts/ep${ep}.old.txt', 'w').write(old_text)
open('docs/superpowers/specs/artifacts/ep${ep}.turbo.txt', 'w').write(new_text)
"
  diff docs/superpowers/specs/artifacts/ep${ep}.old.txt \
       docs/superpowers/specs/artifacts/ep${ep}.turbo.txt \
       > docs/superpowers/specs/artifacts/ep${ep}.diff || true
done
```

Review each `.diff` file manually. Accept turbo if **all three** hold:

1. No systematic drops of speaker names, show regulars (Chas, Dave, Matt, Juan), or domain proper nouns (Guatemala, CICIG, Cuba, etc.).
2. No missing segments or chunks longer than ~30 seconds with no text.
3. A subjective read of 2–3 random 5-minute windows per episode reveals no obvious quality cliff.

- [ ] **Step 8a: If turbo quality PASSES — keep the commit and proceed to Task 5**

Nothing to do. Move on.

- [ ] **Step 8b: If turbo quality FAILS — revert the model swap, keep the GPU upgrade**

```bash
git revert HEAD --no-edit
modal deploy cloud/transcribe_modal.py
```

Then skip the Whisper-model reference update in Task 5 — the model stayed `large-v3`, only the GPU tier line needs updating.

---

## Task 5: Update `CLAUDE.md` with the new performance characteristics

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the transcription bullet in the "Key design decisions" section of `CLAUDE.md`**

Find the bullet that starts with `**Cloud transcription**:` and replace the current line:

```markdown
- **Cloud transcription**: Transcription runs on a Modal L4 GPU (`cloud/transcribe_modal.py`) using `faster-whisper large-v3`. Modal fetches audio from the RSS enclosure URL — no local audio download. Model weights (~3 GB) persist in a `modal.Volume` (`pep-oracle-whisper-cache`) so cold starts only reseed on first deploy. Cost ~$0.07–0.13 per 2-hour episode; wall-clock ~5–10 min. Deploy with `modal deploy cloud/transcribe_modal.py`. Fail-fast on Modal errors (no fallback). Cache format at `~/.pep-oracle/cache/transcripts/{guid}.whisper.json` is unchanged from the OpenAI-era so pre-existing caches still load.
```

with (assuming turbo passed in Task 4; if turbo was reverted, keep `large-v3` in the text):

```markdown
- **Cloud transcription**: Transcription runs on a Modal A100 GPU (`cloud/transcribe_modal.py`) using `faster-whisper large-v3-turbo`. Modal fetches audio from the RSS enclosure URL — no local audio download. Model weights persist in a `modal.Volume` (`pep-oracle-whisper-cache`) so cold starts only reseed on first deploy. Wall-clock ~1 min per 2-hour episode; runs concurrently with diarization from `ingest.py`. Deploy with `modal deploy cloud/transcribe_modal.py`. Fail-fast on Modal errors (no fallback). Cache format at `~/.pep-oracle/cache/transcripts/{guid}.whisper.json` is unchanged.
```

- [ ] **Step 2: Update the diarization bullet**

Find the bullet starting `**Cloud diarization**:` and change the GPU reference:

Find: `Speaker diarization runs on a Modal L4 GPU`
Change to: `Speaker diarization runs on a Modal A100 GPU`

Find: `Diarization cost is ~$0.05 per 2-hour episode and takes ~5 minutes.`
Change to: `Diarization takes ~2–3 min per 2-hour episode and runs concurrently with transcription.`

- [ ] **Step 3: Update the architecture section to reflect concurrent execution**

Find the paragraph starting `**Ingestion** (\`ingest.py\` orchestrates):` and replace the `transcripts/manager.py ... → optional transcripts/diarize.py ...` portion:

Find:
```markdown
`feed.py` (RSS parse) → `transcripts/manager.py` (transcription via Modal GPU, `cloud/transcribe_modal.py`, with caching) → optional `transcripts/diarize.py` (diarization via Modal GPU, `cloud/diarize_modal.py`) → `chunking.py` (time-window chunks with overlap) → `embeddings.py` (local fastembed / bge-large) → `store.py` (ChromaDB upsert).
```

Change to:
```markdown
`feed.py` (RSS parse) → **concurrent**: [`transcripts/manager.py` (Whisper via `cloud/transcribe_modal.py`) ‖ `transcripts/diarize.py` `get_speaker_segments` (pyannote via `cloud/diarize_modal.py`)] → `apply_diarization` (aligns speakers to transcript) → `chunking.py` (time-window chunks with overlap) → `embeddings.py` (local fastembed / bge-large) → `store.py` (ChromaDB upsert).
```

- [ ] **Step 4: Re-run the test suite as a sanity check**

Run: `uv run pytest -x -q`
Expected: all PASS (docs changes shouldn't affect tests, but the commit hook runs pytest).

- [ ] **Step 5: Commit CLAUDE.md (and touch the claude-md-reviewed marker)**

```bash
# The commit hook requires the claude-md-improver marker to be fresh.
# Skill invocation: run /claude-md-improver if the commit hook complains.
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for concurrent ingest + A100 + turbo Whisper"
```

If the pre-commit hook blocks on the CLAUDE.md review marker, run `/claude-md-improver` first, then re-run the commit.

---

## Self-Review

**Spec coverage check:**

| Spec section | Implementing task(s) |
|---|---|
| Concurrent Modal orchestration in `ingest.py` | Task 2 (with Task 1 as a prerequisite refactor) |
| Whisper model swap (large-v3 → large-v3-turbo) | Task 4 |
| GPU tier upgrade (L4 → A100 both functions) | Task 3 |
| Per-stage duration logging | Task 2 (Steps 5-6) |
| Unit test for concurrent helper (max not sum) | Task 2 (Steps 1-2, 7) |
| Turbo quality validation on 3 episodes | Task 4 (Steps 5-7) |
| Rollback path if turbo quality fails | Task 4 (Step 8b) |
| CLAUDE.md updates (cost, wall-clock, model) | Task 5 |

All spec requirements are covered.

**Execution ordering rationale:** Task 3 (GPU upgrade) lands before Task 4 (Whisper swap) so each change can be rolled back independently. Task 1 (refactor) lands before Task 2 (concurrent) because Task 2 calls `get_speaker_segments` directly. Task 5 (docs) is last so CLAUDE.md reflects the final state.

---

## Success Criteria (run after Task 5)

- [ ] **End-to-end cache-miss ingest of a 2-hour episode completes in ≤ 5 min** — verify by timing one `pep-oracle ingest --episode <N> --force --diarize` run and reading the `critical path=<Z>s` log line.
- [ ] **Cache-hit re-ingest wall-clock is unchanged from before this work** — time a `--force` re-ingest and compare against the pre-change baseline (both should be near-instant since caches short-circuit Modal).
- [ ] **Transcript quality on the 3-episode turbo comparison was judged acceptable** — or the turbo commit was reverted.
- [ ] **`uv run pytest -x -q` passes** in the final state.
