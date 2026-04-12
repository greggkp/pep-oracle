# Scoped Ingest with Progress — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the GUI's "Ingest now" button only ingest the episodes shown as not-ingested, and show real-time progress that survives page refresh.

**Architecture:** Add `episode_numbers` filter to `ingest_all()` and the `/ingest` endpoint. Add `progress_callback` to `ingest_all()`, `_ingest_one()`, `get_transcript()`, and `transcribe_episode()` to report step-level progress. Server tracks progress in a module-level dict exposed via `/ingest/status`. GUI polls and displays progress, including on page load.

**Tech Stack:** FastAPI, pytest, JavaScript

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `src/pep_oracle/ingest.py:21-88` | Add `episode_numbers` filter, `progress_callback` to `ingest_all` and `_ingest_one` |
| Modify | `src/pep_oracle/transcripts/manager.py:31-60` | Thread `progress_callback` through `get_transcript` |
| Modify | `src/pep_oracle/transcripts/whisper.py:72-101` | Thread `progress_callback` through `transcribe_episode` |
| Modify | `src/pep_oracle/server.py:20-23,32-33,142-168` | `IngestRequest.episode_numbers`, progress state, callback wiring, `/ingest/status` response |
| Modify | `src/pep_oracle/web/index.html:413-466` | Send `episode_numbers`, show progress, poll on load |
| Modify | `tests/test_ingest.py` | Test `episode_numbers` filter, test `progress_callback` |

---

### Task 1: Add episode_numbers filter and progress_callback to ingest.py

**Files:**
- Modify: `src/pep_oracle/ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Add test for episode_numbers filtering**

Append to `tests/test_ingest.py`:

```python
@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_all_filters_by_episode_numbers(mock_embed, mock_transcript, mock_fetch):
    """When episode_numbers is provided, only those episodes are processed."""
    collection = _fresh_collection()
    mock_fetch.return_value = [_make_episode(1), _make_episode(2), _make_episode(3)]

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value=set()),
    ):
        result = ingest_all(confirm_cost=False, episode_numbers=[2, 3])

    assert result["processed"] == 2
    assert result["failed"] == 0
    guids = get_ingested_guids(collection)
    assert guids == {"guid-2", "guid-3"}
    # Episode 1 should not have been transcribed
    call_eps = [call[0][0].episode_number for call in mock_transcript.call_args_list]
    assert 1 not in call_eps
```

- [ ] **Step 2: Add test for progress_callback**

Append to `tests/test_ingest.py`:

```python
@patch("pep_oracle.ingest.fetch_episodes")
@patch("pep_oracle.ingest.get_transcript", return_value=(FAKE_SEGMENTS, "whisper_cached"))
@patch("pep_oracle.ingest.embed_texts", side_effect=_fake_embed)
def test_ingest_all_calls_progress_callback(mock_embed, mock_transcript, mock_fetch):
    """progress_callback should be called with episode and step info."""
    collection = _fresh_collection()
    mock_fetch.return_value = [_make_episode(1)]
    calls = []

    with (
        patch("pep_oracle.ingest.get_client"),
        patch("pep_oracle.ingest.get_collection", return_value=collection),
        patch("pep_oracle.ingest.get_ingested_guids", return_value=set()),
    ):
        result = ingest_all(confirm_cost=False, progress_callback=calls.append)

    assert result["processed"] == 1
    # Should have at least an episode-start call and step calls
    assert any("Ep 1" in c for c in calls)
    assert any("embedding" in c.lower() or "storing" in c.lower() for c in calls)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_ingest.py::test_ingest_all_filters_by_episode_numbers tests/test_ingest.py::test_ingest_all_calls_progress_callback -v`
Expected: Both FAIL — `ingest_all` doesn't accept `episode_numbers` or `progress_callback` yet.

- [ ] **Step 4: Implement episode_numbers and progress_callback in ingest.py**

In `src/pep_oracle/ingest.py`, replace `_ingest_one` (lines 21-41):

```python
def _ingest_one(episode: Episode, collection, force: bool = False, progress_callback=None) -> bool:
    """Ingest a single episode. Returns True on success."""
    label = f"Ep {episode.episode_number or '?'}: {episode.title[:50]}"

    if force:
        delete_episode(collection, episode.guid)

    if progress_callback:
        progress_callback("transcribing")
    segments, source = get_transcript(episode, progress_callback=progress_callback)
    click.echo(f"  Transcript: {source} ({len(segments)} segments)")

    chunks = chunk_transcript(segments, episode)
    if not chunks:
        click.echo(f"  Skipped (no excerpts produced)")
        return False

    if progress_callback:
        progress_callback(f"embedding {len(chunks)} excerpts")
    click.echo(f"  Embedding {len(chunks)} excerpts...", nl=False)
    embeddings = embed_texts([c.text for c in chunks])
    click.echo(" done")
    if progress_callback:
        progress_callback(f"storing {len(chunks)} excerpts")
    add_chunks(collection, chunks, embeddings)
    click.echo(f"  Stored {len(chunks)} excerpts")
    return True
```

Replace `ingest_all` (lines 44-88):

```python
def ingest_all(force: bool = False, confirm_cost: bool = True, episode_numbers: list[int] | None = None, progress_callback=None) -> dict:
    """Ingest all episodes. Returns summary stats."""
    episodes = fetch_episodes()
    client = get_client()
    collection = get_collection(client)
    ingested_guids = get_ingested_guids(collection)

    if force:
        to_process = episodes
    else:
        to_process = [ep for ep in episodes if ep.guid not in ingested_guids]

    if episode_numbers:
        ep_set = set(episode_numbers)
        to_process = [ep for ep in to_process if ep.episode_number in ep_set]

    if not to_process:
        click.echo("All episodes already ingested.")
        return {"processed": 0, "skipped": len(episodes), "failed": 0}

    already = len(episodes) - len(to_process)
    click.echo(f"{len(to_process)} episodes to process ({already} already ingested)")

    # Estimate cost for episodes that will need Whisper
    if confirm_cost:
        cost = estimate_whisper_cost(to_process)
        if cost > 0.50:
            click.echo(f"Estimated max Whisper cost: ${cost:.2f}")
            click.echo("(Episodes with Apple transcripts will be free)")
            if not click.confirm("Proceed?"):
                return {"processed": 0, "skipped": len(episodes), "failed": 0}

    # Process oldest first
    to_process.sort(key=lambda ep: ep.pub_date)

    succeeded = 0
    failed = 0
    for i, episode in enumerate(to_process, 1):
        label = f"Ep {episode.episode_number or '?'}"
        click.echo(f"[{i}/{len(to_process)}] {label}: {episode.title[:60]}")
        if progress_callback:
            progress_callback(f"[{i}/{len(to_process)}] {label}: {episode.title[:60]}")
        try:
            if _ingest_one(episode, collection, force=force, progress_callback=progress_callback):
                succeeded += 1
        except Exception as e:
            click.echo(f"  FAILED: {e}")
            failed += 1

    click.echo(f"\nDone: {succeeded} ingested, {failed} failed, {already} already up-to-date")
    return {"processed": succeeded, "skipped": already, "failed": failed}
```

- [ ] **Step 5: Run the new tests**

Run: `uv run pytest tests/test_ingest.py::test_ingest_all_filters_by_episode_numbers tests/test_ingest.py::test_ingest_all_calls_progress_callback -v`
Expected: Both PASS.

- [ ] **Step 6: Run all ingest tests**

Run: `uv run pytest tests/test_ingest.py -v`
Expected: All tests pass (existing tests unaffected since new params are optional).

- [ ] **Step 7: Commit**

```bash
git add src/pep_oracle/ingest.py tests/test_ingest.py
git commit -m "feat: add episode_numbers filter and progress_callback to ingest_all"
```

---

### Task 2: Thread progress_callback through transcript pipeline

**Files:**
- Modify: `src/pep_oracle/transcripts/manager.py:31-60`
- Modify: `src/pep_oracle/transcripts/whisper.py:72-101`

- [ ] **Step 1: Add progress_callback to get_transcript in manager.py**

In `src/pep_oracle/transcripts/manager.py`, replace the `get_transcript` function (lines 31-60):

```python
def get_transcript(
    episode: Episode,
    delete_audio_after: bool = True,
    progress_callback=None,
) -> tuple[list[TranscriptSegment], str]:
    """Get transcript for an episode via Whisper.

    Returns (segments, source) where source is "whisper" or "whisper_cached".
    """
    # Check Whisper cache first (cheapest check)
    if _has_cached_whisper_transcript(episode):
        from pep_oracle.transcripts.whisper import _load_cached
        cache_path = TRANSCRIPT_CACHE_DIR / f"{episode.guid}.whisper.json"
        return _load_cached(cache_path), "whisper_cached"

    # Whisper transcription
    if not episode.audio_url:
        raise RuntimeError(f"No audio URL for episode: {episode.title}")

    if progress_callback:
        progress_callback("downloading audio")
    click.echo("  Downloading audio...", nl=False)
    audio_path = download_audio(episode)
    size_mb = audio_path.stat().st_size / 1_000_000
    click.echo(f" {size_mb:.0f} MB")

    try:
        segments = transcribe_episode(audio_path, episode.guid, progress_callback=progress_callback)
    finally:
        if delete_audio_after and audio_path.exists():
            audio_path.unlink()

    return segments, "whisper"
```

- [ ] **Step 2: Add progress_callback to transcribe_episode in whisper.py**

In `src/pep_oracle/transcripts/whisper.py`, replace the `transcribe_episode` function (lines 72-101):

```python
def transcribe_episode(audio_path: Path, episode_guid: str, client: OpenAI | None = None, progress_callback=None) -> list[TranscriptSegment]:
    """Transcribe an audio file, splitting if needed. Caches the result."""
    ensure_dirs()

    cache_path = TRANSCRIPT_CACHE_DIR / f"{episode_guid}.whisper.json"
    if cache_path.exists():
        return _load_cached(cache_path)

    if client is None:
        client = OpenAI()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        if progress_callback:
            progress_callback("splitting audio")
        click.echo("  Splitting audio...", nl=False)
        chunks = split_audio(audio_path, tmp_path)
        click.echo(f" {len(chunks)} parts")

        duration = _get_duration_seconds(audio_path) if len(chunks) > 1 else None
        chunk_duration = duration / len(chunks) if duration else None

        all_segments: list[TranscriptSegment] = []
        for i, (chunk_path, offset) in enumerate(chunks):
            dur_label = f"{chunk_duration / 60:.0f} min" if chunk_duration else "?"
            if progress_callback:
                progress_callback(f"transcribing part {i + 1}/{len(chunks)}")
            click.echo(f"  Transcribing part {i + 1}/{len(chunks)} ({dur_label})...", nl=False)
            new_segments = transcribe_chunk(chunk_path, offset, client)
            all_segments.extend(new_segments)
            click.echo(f" {len(new_segments)} segments")

    _save_cache(all_segments, cache_path)
    return all_segments
```

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest -x -q`
Expected: All tests pass (new params are optional, existing callers unchanged).

- [ ] **Step 4: Commit**

```bash
git add src/pep_oracle/transcripts/manager.py src/pep_oracle/transcripts/whisper.py
git commit -m "feat: thread progress_callback through transcript pipeline"
```

---

### Task 3: Wire up progress tracking in server.py

**Files:**
- Modify: `src/pep_oracle/server.py:20-23,32-33,142-168`

- [ ] **Step 1: Add episode_numbers to IngestRequest and progress state**

In `src/pep_oracle/server.py`, replace lines 20-23:

```python
_ingest_lock = asyncio.Lock()
_ingest_running = False
_ingest_last_result: dict | None = None
```

With:

```python
_ingest_lock = asyncio.Lock()
_ingest_running = False
_ingest_last_result: dict | None = None
_ingest_progress: dict = {"current_episode": "", "episodes_done": 0, "episodes_total": 0, "step": ""}
```

Replace `IngestRequest` (lines 32-33):

```python
class IngestRequest(BaseModel):
    force: bool = False
    episode_numbers: list[int] = []
```

- [ ] **Step 2: Update /ingest endpoint with progress callback**

Replace the `/ingest` endpoint (lines 142-164):

```python
@app.post("/ingest")
async def api_ingest(req: IngestRequest):
    global _ingest_running, _ingest_last_result, _ingest_progress

    if _ingest_running:
        return {"status": "already_running"}

    async def _run():
        global _ingest_running, _ingest_last_result, _ingest_progress
        try:
            def _progress(step: str):
                global _ingest_progress
                # Episode-level messages look like "[1/3] Ep 255: TITLE..."
                if step.startswith("["):
                    parts = step.split("] ", 1)
                    counts = parts[0].lstrip("[")
                    done, total = counts.split("/")
                    _ingest_progress["episodes_done"] = int(done) - 1
                    _ingest_progress["episodes_total"] = int(total)
                    _ingest_progress["current_episode"] = parts[1] if len(parts) > 1 else ""
                    _ingest_progress["step"] = "starting"
                else:
                    _ingest_progress["step"] = step

            result = await asyncio.to_thread(
                ingest_all,
                force=req.force,
                confirm_cost=False,
                episode_numbers=req.episode_numbers or None,
                progress_callback=_progress,
            )
            _ingest_last_result = result
        except Exception as e:
            _ingest_last_result = {"error": str(e)}
            logger.exception("Ingestion failed")
        finally:
            _ingest_running = False
            _ingest_progress = {"current_episode": "", "episodes_done": 0, "episodes_total": 0, "step": ""}

    _ingest_running = True
    _ingest_progress = {"current_episode": "", "episodes_done": 0, "episodes_total": 0, "step": ""}
    asyncio.create_task(_run())
    return {"status": "started"}
```

- [ ] **Step 3: Update /ingest/status response**

Replace the `/ingest/status` endpoint (lines 167-168):

```python
@app.get("/ingest/status")
async def api_ingest_status():
    return {"running": _ingest_running, "last_result": _ingest_last_result, **_ingest_progress}
```

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest -x -q`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/server.py
git commit -m "feat: wire up scoped ingest and progress tracking in server"
```

---

### Task 4: Update GUI to send episode_numbers and show progress

**Files:**
- Modify: `src/pep_oracle/web/index.html`

- [ ] **Step 1: Update ingest button to send episode_numbers**

In `src/pep_oracle/web/index.html`, find the ingest button click handler (~line 413-436). Replace:

```javascript
      const resp = await fetch("/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: false }),
      });
```

With:

```javascript
      const resp = await fetch("/ingest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: false, episode_numbers: notIngestedEpisodes }),
      });
```

- [ ] **Step 2: Update pollIngestion to show progress detail**

Replace the `pollIngestion` function (~lines 438-466):

```javascript
  function pollIngestion() {
    const timer = setInterval(async () => {
      try {
        const resp = await fetch("/ingest/status");
        const data = await resp.json();
        if (!data.running) {
          clearInterval(timer);
          if (data.last_result && data.last_result.error) {
            ingestBannerText.textContent = "Ingestion failed: " + data.last_result.error;
            ingestBtn.style.display = "";
            ingestBtn.disabled = false;
            ingestBtn.textContent = "Retry";
          } else {
            ingestBanner.style.display = "none";
            // Remove not-ingested styling from chips
            topicsEl.querySelectorAll(".topic-chip.not-ingested").forEach(chip => {
              chip.classList.remove("not-ingested");
              chip.title = chip.title.replace(" (not yet ingested)", "");
            });
            notIngestedEpisodes = [];
            // Refresh status bar
            loadStatus();
          }
        } else {
          // Show progress detail
          let progressText = "Ingesting\u2026";
          if (data.current_episode) {
            const total = data.episodes_total || "?";
            const done = (data.episodes_done || 0) + 1;
            progressText = "Ingesting " + data.current_episode + " (" + done + "/" + total + ")";
            if (data.step && data.step !== "starting") {
              progressText += ": " + data.step;
            }
          }
          ingestBannerText.textContent = progressText;
        }
      } catch (e) {
        // keep polling
      }
    }, 3000);
  }
```

- [ ] **Step 3: Add page-load ingestion check**

After the `pollIngestion` function, add a check that runs on page load. Find the line that calls `loadStatus()` and `loadTopics()` near the bottom of the script. After those calls, add:

```javascript
  // Check if ingestion is already running (e.g., page was refreshed)
  async function checkIngestionOnLoad() {
    try {
      const resp = await fetch("/ingest/status");
      const data = await resp.json();
      if (data.running) {
        ingestBtn.style.display = "none";
        ingestBanner.style.display = "flex";
        ingestBannerText.textContent = "Ingesting\u2026";
        pollIngestion();
      }
    } catch (e) {
      // silent
    }
  }
  checkIngestionOnLoad();
```

- [ ] **Step 4: Start the server and test manually in browser**

Start: `uv run pep-oracle-server &`
Open: `http://localhost:8000`
Verify:
1. If not-ingested episodes exist, the banner shows with "Ingest now" button
2. Clicking "Ingest now" shows progress (episode name, step detail)
3. Refreshing the page while ingesting still shows the progress banner
4. When ingestion completes, banner disappears and status refreshes

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest -x -q`
Expected: All tests pass.

- [ ] **Step 6: Commit all remaining changes**

```bash
git add src/pep_oracle/web/index.html docs/superpowers/specs/2026-04-11-scoped-ingest-with-progress-design.md docs/superpowers/plans/2026-04-11-scoped-ingest-with-progress.md
git commit -m "feat: GUI sends scoped episode list and shows ingestion progress"
```
