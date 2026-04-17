# Modal Cloud Diarization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace local pyannote diarization (slow on OptiPlex Micro CPU) with a Modal GPU-hosted diarization function. Reduces per-episode ingestion from ~1-2hr to ~5-7min.

**Architecture:** A single Modal function (`cloud/diarize_modal.py`) runs pyannote 3.1 on an L4 GPU, downloading audio directly from the podcast RSS enclosure URL. Client code looks up the deployed function and calls `.remote()`. No local fallback. All chunked-diarization code is removed since GPU VRAM handles a 2-hour episode in one pass.

**Tech Stack:** Python 3.12, Modal (cloud GPU), pyannote.audio 3.3, existing FastAPI server + ChromaDB pipeline.

**Spec:** `docs/superpowers/specs/2026-04-17-modal-cloud-diarization-design.md`

---

### Task 1: Dependencies and environment config

**Files:**
- Modify: `pyproject.toml`
- Modify: `.env.example`

- [ ] **Step 1: Add `modal` to main deps in pyproject.toml**

Open `pyproject.toml` and locate the `dependencies = [...]` list under `[project]`. Add `"modal>=0.64,<1"`. Locate `[project.optional-dependencies]` and **remove** the `diarize = ["pyannote.audio", "torch", "torchaudio"]` entry (pyannote no longer runs locally).

Expected final state of the extras section (nothing left if `diarize` was the only extra — delete the whole `[project.optional-dependencies]` block if so, or leave any other extras untouched).

- [ ] **Step 2: Update lockfile and sync deps**

Run: `uv lock && uv pip install -e .`
Expected: `uv.lock` updates to reflect the new/removed deps, venv installs `modal`, and `pyannote.audio`/`torch`/`torchaudio` are dropped from the venv if present.

- [ ] **Step 3: Add Modal credentials to .env.example**

Append to `.env.example`:

```
# Modal credentials for cloud diarization (from `modal token new`)
MODAL_TOKEN_ID=ak-...
MODAL_TOKEN_SECRET=as-...
```

- [ ] **Step 4: Run full test suite to verify nothing broke**

Run: `uv run pytest -x -q`
Expected: 220 passed. No tests should fail from removing the `[diarize]` extra because the tests mock pyannote imports.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .env.example uv.lock
git commit -m "chore: add modal dep, remove pyannote extra"
```

---

### Task 2: Write the Modal function

**Files:**
- Create: `cloud/diarize_modal.py`

- [ ] **Step 1: Create the cloud directory and Modal function**

Create `cloud/diarize_modal.py` with exactly this content:

```python
"""Modal cloud function for speaker diarization.

Deploy with: modal deploy cloud/diarize_modal.py
"""
import modal

app = modal.App("pep-oracle-diarize")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "pyannote.audio==3.3.*",
        "numpy",
        "soundfile",
        "torch",
        "torchaudio",
    )
)

hf_secret = modal.Secret.from_name("huggingface-token")


@app.function(
    image=image,
    gpu="L4",
    secrets=[hf_secret],
    timeout=1800,
)
def diarize(audio_url: str, num_speakers: int | None = None) -> list[dict]:
    """Download audio from a URL and run pyannote 3.1 on GPU.

    Returns a list of {"speaker": str, "start": float, "end": float} dicts
    sorted by start time.
    """
    import os
    import tempfile
    import urllib.request
    from pathlib import Path

    from pyannote.audio import Pipeline

    hf_token = os.environ["HF_TOKEN"]
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=hf_token,
    )
    pipeline.to(__import__("torch").device("cuda"))

    with tempfile.TemporaryDirectory() as td:
        audio_path = Path(td) / "audio.mp3"
        try:
            urllib.request.urlretrieve(audio_url, audio_path)
        except Exception as e:
            raise RuntimeError(f"audio fetch failed: {e}") from e

        kwargs = {}
        if num_speakers is not None:
            kwargs["num_speakers"] = num_speakers
        result = pipeline(str(audio_path), **kwargs)

    # pyannote ≥3.3 returns DiarizeOutput; unwrap to Annotation
    diarization = getattr(result, "speaker_diarization", result)
    return [
        {"speaker": speaker, "start": float(turn.start), "end": float(turn.end)}
        for turn, _, speaker in diarization.itertracks(yield_label=True)
    ]
```

- [ ] **Step 2: Verify the file is syntactically valid**

Run: `uv run python -c "import ast; ast.parse(open('cloud/diarize_modal.py').read())"`
Expected: no output (exit 0).

- [ ] **Step 3: Commit**

```bash
git add cloud/diarize_modal.py
git commit -m "feat: modal function for GPU diarization"
```

---

### Task 3: Document cloud deploy workflow

**Files:**
- Create: `cloud/README.md`

- [ ] **Step 1: Write the cloud README**

Create `cloud/README.md`:

```markdown
# Cloud functions

## diarize_modal.py

Speaker diarization on a Modal L4 GPU. Replaces local pyannote for the ingestion pipeline.

### One-time setup

1. Install Modal: `uv pip install modal`
2. Authenticate: `modal token new` (opens browser)
3. Create HuggingFace secret:
   ```
   modal secret create huggingface-token HF_TOKEN=<your-hf-token>
   ```
   Token must have access to `pyannote/speaker-diarization-3.1` (accept the license at https://huggingface.co/pyannote/speaker-diarization-3.1).
4. Deploy: `modal deploy cloud/diarize_modal.py`
5. Copy `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` from `~/.modal/token` into `/opt/pep-oracle/app/.env`.
6. Restart the pep-oracle server.

### Redeploy

When `diarize_modal.py` changes: `modal deploy cloud/diarize_modal.py`. The client code looks up the deployed function by name at call time; no client change needed.

### Cost

~$0.05 per 2-hour episode on L4 ($0.80/hr, ~5 min per episode).
```

- [ ] **Step 2: Commit**

```bash
git add cloud/README.md
git commit -m "docs: cloud function deploy instructions"
```

---

### Task 4: Rewrite `diarize_audio` to call Modal (TDD)

**Files:**
- Modify: `src/pep_oracle/transcripts/diarize.py`
- Modify: `tests/test_diarize.py`

- [ ] **Step 1: Add new test for the Modal call (at the end of `tests/test_diarize.py`)**

Append to `tests/test_diarize.py`:

```python
def test_diarize_audio_calls_modal(monkeypatch):
    """diarize_audio looks up the deployed Modal function and returns parsed segments."""
    from pep_oracle.transcripts import diarize as diarize_module

    calls = []

    class FakeRemote:
        def remote(self, audio_url, num_speakers):
            calls.append((audio_url, num_speakers))
            return [
                {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.5},
                {"speaker": "SPEAKER_01", "start": 5.5, "end": 10.0},
            ]

    class FakeModal:
        class Function:
            @staticmethod
            def from_name(app_name, func_name):
                assert app_name == "pep-oracle-diarize"
                assert func_name == "diarize"
                return FakeRemote()

    monkeypatch.setattr(diarize_module, "modal", FakeModal)

    result = diarize_module.diarize_audio("https://example.com/ep.mp3", num_speakers=2)

    assert calls == [("https://example.com/ep.mp3", 2)]
    assert len(result) == 2
    assert result[0].speaker == "SPEAKER_00"
    assert result[0].start == 0.0
    assert result[0].end == 5.5
    assert result[1].speaker == "SPEAKER_01"


def test_diarize_audio_no_num_speakers(monkeypatch):
    """num_speakers defaults to None."""
    from pep_oracle.transcripts import diarize as diarize_module

    received = {}

    class FakeRemote:
        def remote(self, audio_url, num_speakers):
            received["num_speakers"] = num_speakers
            return []

    class FakeModal:
        class Function:
            @staticmethod
            def from_name(app_name, func_name):
                return FakeRemote()

    monkeypatch.setattr(diarize_module, "modal", FakeModal)

    diarize_module.diarize_audio("https://example.com/ep.mp3")
    assert received["num_speakers"] is None
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_diarize.py::test_diarize_audio_calls_modal tests/test_diarize.py::test_diarize_audio_no_num_speakers -v`
Expected: both tests FAIL because `diarize_module.modal` doesn't exist yet, or `diarize_audio` has the old `audio_path: Path` signature.

- [ ] **Step 3: Replace `diarize_audio` and add the `modal` import in `src/pep_oracle/transcripts/diarize.py`**

At the top of the file, add `import modal` alongside the existing imports (after the stdlib imports, before third-party imports like `click`):

```python
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import click
import modal

from pep_oracle.config import DIARIZATION_CACHE_DIR, SPEAKER_PROFILES_PATH, ensure_dirs
from pep_oracle.models import TranscriptSegment
```

Remove these imports if present: `subprocess`, `tempfile` (they were only used by the chunking helpers we're deleting in Task 5).

Replace the entire `diarize_audio` function body with:

```python
def diarize_audio(
    audio_url: str,
    num_speakers: int | None = None,
) -> list[SpeakerSegment]:
    """Run pyannote diarization on a Modal GPU. Returns parsed speaker segments."""
    f = modal.Function.from_name("pep-oracle-diarize", "diarize")
    raw = f.remote(audio_url, num_speakers)
    return [SpeakerSegment(**r) for r in raw]
```

Do NOT delete the helper functions yet (`_audio_duration_seconds`, `_load_pipeline`, `_run_pipeline`, `_diarize_chunked`, `_stitch_equivalences`, `_activity_by_label`, `_turns_overlap`, `_relabel_and_merge`, `CHUNK_SECONDS`, `CHUNK_OVERLAP_SECONDS`) — those come out in Task 5. This task just rewires `diarize_audio`.

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_diarize.py::test_diarize_audio_calls_modal tests/test_diarize.py::test_diarize_audio_no_num_speakers -v`
Expected: both PASS.

- [ ] **Step 5: Run the full diarize test suite to verify nothing else broke**

Run: `uv run pytest tests/test_diarize.py -v`
Expected: all existing tests still pass (they test align/map/cache — unaffected by the `diarize_audio` swap).

- [ ] **Step 6: Commit**

```bash
git add src/pep_oracle/transcripts/diarize.py tests/test_diarize.py
git commit -m "feat: diarize_audio calls Modal instead of local pyannote"
```

---

### Task 5: Delete dead chunking code and tests

**Files:**
- Modify: `src/pep_oracle/transcripts/diarize.py`
- Modify: `tests/test_diarize.py`

- [ ] **Step 1: Delete the chunking helpers from `src/pep_oracle/transcripts/diarize.py`**

Delete these top-level items from the file:

- `CHUNK_SECONDS = 1500` (constant, near top)
- `CHUNK_OVERLAP_SECONDS = 30` (constant)
- `_audio_duration_seconds()` function
- `_load_pipeline()` function
- `_run_pipeline()` function
- `_diarize_chunked()` function
- `_stitch_equivalences()` function
- `_activity_by_label()` function
- `_turns_overlap()` function
- `_relabel_and_merge()` function

Also delete the comment block above `CHUNK_SECONDS`:
```
# Audio longer than this (seconds) is diarized in chunks to bound peak RAM —
# pyannote loads the full waveform + embeddings into memory, so a 2-hour
# episode would use ~7 GB RSS. Chunked processing caps it at ~2 GB.
```

Verify nothing else in the file references the deleted names:
Run: `grep -E "CHUNK_SECONDS|CHUNK_OVERLAP|_audio_duration|_load_pipeline|_run_pipeline|_diarize_chunked|_stitch_equivalences|_activity_by_label|_turns_overlap|_relabel_and_merge" src/pep_oracle/transcripts/diarize.py`
Expected: no matches.

- [ ] **Step 2: Delete the chunking tests from `tests/test_diarize.py`**

Delete these test functions:

- `test_activity_by_label_clips_to_window`
- `test_turns_overlap_sums_pairwise`
- `test_stitch_equivalences_swapped_labels`
- `test_stitch_equivalences_empty_overlap`
- `test_relabel_and_merge_unions_equivalent_speakers`
- `test_relabel_and_merge_merges_adjacent_same_speaker`
- `test_relabel_and_merge_preserves_concurrent_speakers`

Also remove any test-file imports that were only used by those tests (check the top of `tests/test_diarize.py` — likely imports of `_activity_by_label`, `_turns_overlap`, `_stitch_equivalences`, `_relabel_and_merge`).

Verify nothing else in the test file references the deleted helpers:
Run: `grep -E "_activity_by_label|_turns_overlap|_stitch_equivalences|_relabel_and_merge" tests/test_diarize.py`
Expected: no matches.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -x -q`
Expected: all remaining tests pass (expect ~213 passed, since we removed 7 tests and added 2).

- [ ] **Step 4: Commit**

```bash
git add src/pep_oracle/transcripts/diarize.py tests/test_diarize.py
git commit -m "refactor: remove local chunked-diarization code"
```

---

### Task 6: Update `diarize_transcript` and callers to pass `audio_url`

**Files:**
- Modify: `src/pep_oracle/transcripts/diarize.py`
- Modify: `src/pep_oracle/ingest.py`
- Modify: `src/pep_oracle/cli.py`

- [ ] **Step 1: Change `diarize_transcript` signature in `src/pep_oracle/transcripts/diarize.py`**

Find the `diarize_transcript` function. Change its parameter `audio_path: Path` to `audio_url: str`. Update the `diarize_audio(audio_path, ...)` call inside to `diarize_audio(audio_url, ...)`.

Before:
```python
def diarize_transcript(
    transcript_segments: list[TranscriptSegment],
    audio_path: Path,
    episode_guid: str,
    num_speakers: int | None = None,
    profile_path: Path | None = None,
    progress_callback=None,
) -> list[TranscriptSegment]:
    ...
    speaker_segments = diarize_audio(audio_path, num_speakers=num_speakers)
```

After:
```python
def diarize_transcript(
    transcript_segments: list[TranscriptSegment],
    audio_url: str,
    episode_guid: str,
    num_speakers: int | None = None,
    profile_path: Path | None = None,
    progress_callback=None,
) -> list[TranscriptSegment]:
    ...
    speaker_segments = diarize_audio(audio_url, num_speakers=num_speakers)
```

Leave the rest of the function (cache lookup, align_speakers, map_speaker_names, profile check) untouched.

- [ ] **Step 2: Update `_ingest_one` in `src/pep_oracle/ingest.py` to stop downloading audio for diarization**

Find the diarize block (around line 42-57):

```python
    if diarize:
        from pep_oracle.transcripts.diarize import diarize_transcript
        from pep_oracle.transcripts.manager import download_audio
        from pep_oracle.config import AUDIO_CACHE_DIR

        audio_path = AUDIO_CACHE_DIR / f"{episode.guid}.mp3"
        if not audio_path.exists():
            audio_path = download_audio(episode)
        try:
            segments = diarize_transcript(
                segments, audio_path, episode.guid,
                progress_callback=progress_callback,
            )
        finally:
            if audio_path.exists():
                audio_path.unlink()
```

Replace with:

```python
    if diarize:
        from pep_oracle.transcripts.diarize import diarize_transcript

        segments = diarize_transcript(
            segments, episode.audio_url, episode.guid,
            progress_callback=progress_callback,
        )
```

Also: above this block, `get_transcript` is called with `delete_audio_after=not diarize` — this is to preserve the local audio for diarization. Since Modal downloads directly from `audio_url`, local audio is no longer needed for diarization. Change that line to `delete_audio_after=True`:

Before:
```python
    segments, source = get_transcript(
        episode, delete_audio_after=not diarize, progress_callback=progress_callback,
    )
```

After:
```python
    segments, source = get_transcript(
        episode, delete_audio_after=True, progress_callback=progress_callback,
    )
```

- [ ] **Step 3: Update `identify_speakers` in `src/pep_oracle/cli.py` to use the URL**

Find the function around line 80. Replace the entire body of the diarization block (the code that downloads audio and calls `diarize_audio`). Before:

```python
    # Download audio if needed
    audio_path = AUDIO_CACHE_DIR / f"{match.guid}.mp3"
    if not audio_path.exists():
        click.echo("Downloading audio...")
        audio_path = download_audio(match)

    # Diarize
    cache_path = DIARIZATION_CACHE_DIR / f"{match.guid}.json"
    if cache_path.exists():
        click.echo("Using cached diarization...")
        speaker_segments = _load_cached(cache_path)
    else:
        click.echo("Diarizing audio (this may take a while)...")
        speaker_segments = diarize_audio(audio_path)
        _save_cache(speaker_segments, cache_path)
```

After:

```python
    # Diarize (Modal downloads from the URL directly)
    cache_path = DIARIZATION_CACHE_DIR / f"{match.guid}.json"
    if cache_path.exists():
        click.echo("Using cached diarization...")
        speaker_segments = _load_cached(cache_path)
    else:
        click.echo("Diarizing audio on Modal...")
        speaker_segments = diarize_audio(match.audio_url)
        _save_cache(speaker_segments, cache_path)
```

Also clean up the no-longer-needed imports at the top of `identify_speakers`:

Before:
```python
    from pep_oracle.config import AUDIO_CACHE_DIR
    from pep_oracle.transcripts.diarize import (
        diarize_audio,
        save_speaker_profiles,
        load_speaker_profiles,
        SpeakerSegment,
        _load_cached,
        _save_cache,
    )
    from pep_oracle.config import DIARIZATION_CACHE_DIR, SPEAKER_PROFILES_PATH, ensure_dirs
    from pep_oracle.transcripts.manager import download_audio
```

After:
```python
    from pep_oracle.transcripts.diarize import (
        diarize_audio,
        save_speaker_profiles,
        load_speaker_profiles,
        SpeakerSegment,
        _load_cached,
        _save_cache,
    )
    from pep_oracle.config import DIARIZATION_CACHE_DIR, SPEAKER_PROFILES_PATH, ensure_dirs
```

(Removed `AUDIO_CACHE_DIR` and `download_audio` imports.)

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest -x -q`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/transcripts/diarize.py src/pep_oracle/ingest.py src/pep_oracle/cli.py
git commit -m "refactor: pass audio_url to diarization instead of local path"
```

---

### Task 7: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the Ingestion pipeline description**

Find the line describing the ingestion pipeline (currently mentions `transcripts/diarize.py (pyannote speaker diarization)`). Replace the existing paragraph with:

```markdown
**Ingestion** (`ingest.py` orchestrates):
`feed.py` (RSS parse) → `transcripts/manager.py` (Whisper with caching) → optional `transcripts/diarize.py` (diarization via Modal GPU, `cloud/diarize_modal.py`) → `chunking.py` (time-window chunks with overlap) → `embeddings.py` (OpenAI batched) → `store.py` (ChromaDB upsert). Web API ingestion runs in a subprocess (`ingest_worker.py`) to isolate ingest failures from the server process.
```

- [ ] **Step 2: Replace the chunked-diarization bullet**

Find the bullet starting "**Chunked diarization**:". Replace it with:

```markdown
- **Cloud diarization**: Speaker diarization runs on a Modal L4 GPU (`cloud/diarize_modal.py`). Modal downloads the audio from the RSS enclosure URL directly — no local audio needed for diarization. Diarization cost is ~$0.05 per 2-hour episode and takes ~5 minutes. Deploy with `modal deploy cloud/diarize_modal.py` after changes. See `cloud/README.md` for one-time setup.
```

- [ ] **Step 3: Update the Speaker diarization bullet**

Find the bullet starting "**Speaker diarization** is optional via CLI". Replace with:

```markdown
- **Speaker diarization**: Optional via CLI (`--diarize` flag), defaults to `True` in the server API. Runs on a Modal GPU — requires `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` env vars. Speaker profiles stored at `~/.pep-oracle/speaker_profiles.json`.
```

- [ ] **Step 4: Update the Environment section**

Under `Optional:`, remove the `HF_TOKEN` line (it lives in Modal's secrets now, not local `.env`). Add:

```markdown
- `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` — Modal credentials for cloud diarization
```

- [ ] **Step 5: Run the full test suite one more time**

Run: `uv run pytest -x -q`
Expected: all tests pass.

- [ ] **Step 6: Touch the CLAUDE.md review marker and commit**

```bash
touch .claude/.md-reviewed
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for Modal diarization"
```

---

### Task 8: Manual deployment and smoke test

**Files:** none (operator task)

- [ ] **Step 1: Install the Modal CLI locally**

Run: `uv pip install modal`

- [ ] **Step 2: Authenticate with Modal**

Run: `modal token new`
Follow the browser prompt. After success, `~/.modal/token` exists.

- [ ] **Step 3: Create the HuggingFace secret on Modal**

Run (substituting the real HF token):
```
modal secret create huggingface-token HF_TOKEN=<your-hf-token>
```

Verify: `modal secret list` shows `huggingface-token`.

- [ ] **Step 4: Deploy the diarization function**

Run: `modal deploy cloud/diarize_modal.py`
Expected: output includes `Deployed app 'pep-oracle-diarize'`.

- [ ] **Step 5: Add Modal credentials to the server's `.env`**

Copy `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` from `~/.modal/token` into `/opt/pep-oracle/app/.env`.

- [ ] **Step 6: Restart the pep-oracle server**

Kill the existing server process and restart it so the new env vars are loaded:

```bash
pkill -f "pep-oracle-server"
sleep 2
uv run pep-oracle-server > /tmp/pep-oracle-server.log 2>&1 &
sleep 3
curl -s http://localhost:8000/status
```

Expected: `{"stale":...}` response (server is up).

- [ ] **Step 7: Trigger a diarized ingest for one episode**

Run:
```bash
curl -s -X POST http://localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{"episode_numbers": [255], "diarize": true, "force": true}'
```

Expected: `{"status":"started"}`.

- [ ] **Step 8: Watch for completion**

Poll `http://localhost:8000/ingest/status` every 60s until `"running": false`. Expected total wall time: ~5-10 minutes.

Check the final result:
```bash
curl -s http://localhost:8000/ingest/status | python3 -m json.tool
```

Expected: `"last_result": {"processed": 1, "skipped": 98, "failed": 0, ...}` (numbers may vary with episode count).

- [ ] **Step 9: Verify via a speaker-filtered query**

```bash
uv run pep-oracle ask "what did Chas say about ceasefires recently"
```

Expected: returns an answer with transcript excerpts attributed to Chas.

- [ ] **Step 10: Sanity-check peak RSS of the ingest worker**

Since diarization is now remote, the local worker's peak RSS should drop significantly (target <500 MB vs the 2.7 GB peak we saw with local chunked pyannote). During the ingest above, run in another shell:

```bash
while pgrep -f pep_oracle.ingest_worker >/dev/null; do
  pid=$(pgrep -f pep_oracle.ingest_worker | head -1)
  awk '/VmRSS/ {print $2/1024 " MB"}' /proc/$pid/status 2>/dev/null
  sleep 10
done
```

Expected: reported RSS stays under ~500 MB throughout.

---

## Notes

- **Commit hygiene**: the commit hook requires `pytest -x -q` pass and `/claude-md-improver` run (touches `.claude/.md-reviewed`) for each commit. Each task above ends with a commit; the CLAUDE.md update in Task 7 is the one that touches the review marker. For intermediate commits (Tasks 1-6) the marker from Task 7's prerequisite needs to remain touched — `touch .claude/.md-reviewed` once before the whole run if needed.
- **Rollback**: if cloud diarization has issues at any point, revert the series with `git revert` — the local pyannote path is available in git history (last seen on `main` at commit `3bc81b8`).
