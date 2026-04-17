# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

pep-oracle is a CLI tool that transcribes and queries the "PEP with Chas and Dr Dave" podcast using RAG. It ingests episodes (transcribed via faster-whisper on Modal), chunks and embeds them, stores in ChromaDB, and answers natural language questions via Claude.

## Commands

```bash
# Setup (uv manages the venv — no activate needed)
uv pip install -e .

# Setup with web server
uv pip install -e ".[server]"

# Run tests (all) — uv run picks up the project venv automatically
uv run pytest

# Run a single test file or test
uv run pytest tests/test_feed.py
uv run pytest tests/test_feed.py::test_parse_duration_hhmmss

# CLI usage
uv run pep-oracle episodes                   # list episodes from RSS
uv run pep-oracle ingest --episode 251       # ingest one episode
uv run pep-oracle ingest                     # ingest all new episodes
uv run pep-oracle ask "question"             # query (auto-detects time/episode context)
uv run pep-oracle status                     # show ingestion stats
uv run pep-oracle export episodes.json       # export all episodes to JSON
uv run pep-oracle export ep.json --episode 251  # export specific episode(s)
uv run pep-oracle import episodes.json       # import episodes from JSON

# Web server
uv run pep-oracle-server                     # starts FastAPI on 0.0.0.0:8000
```

## Architecture

Two pipelines, both orchestrated through `cli.py`. Web UI via `server.py` (FastAPI) serving `src/pep_oracle/web/index.html`.

**Ingestion** (`ingest.py` orchestrates):
`feed.py` (RSS parse) → `transcripts/manager.py` (transcription via Modal GPU, `cloud/transcribe_modal.py`, with caching) → optional `transcripts/diarize.py` (diarization via Modal GPU, `cloud/diarize_modal.py`) → `chunking.py` (time-window chunks with overlap) → `embeddings.py` (OpenAI batched) → `store.py` (ChromaDB upsert). Web API ingestion runs in a subprocess (`ingest_worker.py`) to isolate ingest failures from the server process.

**Query** (`query.py` orchestrates):
Pre-process question via Claude Haiku (extract date/episode/speaker filters + recency intent) → embed search query (OpenAI) → retrieve top-k chunks from ChromaDB (with filters + optional recency re-ranking + optional speaker filtering) → trim chunks to target speaker's portions if speaker filter active (`_trim_to_speaker`) → build prompt with transcript excerpts sorted newest-first → send to Claude → render with rich Markdown. Compare queries ("Chas vs Dave on X") run dual retrieval (one per speaker, `top_k/2` each) with labeled context sections.

**Topic chips** (`topics.py`):
Topics are extracted deterministically from episode show notes at ingestion time: `parse_description_topics()` extracts timestamp labels → `clean_episode_topics()` strips segment prefixes (Correspondence, Not Normal, Stats Nug, Policy Time), extracts parenthetical subtopics, cleans Unleashed entries, and strips Cont. suffixes → `_ingest_one()` returns the topic entry; callers (`ingest_all`, `ingest_episode`) batch entries and call `save_topics()` once → persisted to `~/.pep-oracle/topics.json`. `/topics` endpoint reads from file — no API call. Frontend renders chips grouped by episode with inline episode numbers ("Cuba · Ep 253"), "More..." button loads older episodes.

## Key design decisions

- **Data transfer between machines**: Use `pep-oracle export` / `import` to move ingested episodes. Never copy ChromaDB files directly — ChromaDB must handle its own writes via upsert to avoid corruption. Export produces a JSON file with chunks, embeddings, and metadata.
- **Data stored at `~/.pep-oracle/`** (cache/transcripts, cache/diarization, chroma), not in the project directory. Override with `PEP_ORACLE_DATA_DIR`. (`cache/audio/` is no longer written — both Modal functions fetch audio directly from `episode.audio_url`. The directory is left alone if it exists from a prior install; `rm -rf` at will.)
- **Incremental ingestion**: episodes tracked by GUID in ChromaDB metadata; already-ingested episodes are skipped unless `--force`.
- **Cloud transcription**: Transcription runs on a Modal L4 GPU (`cloud/transcribe_modal.py`) using `faster-whisper large-v3`. Modal fetches audio from the RSS enclosure URL — no local audio download. Model weights (~3 GB) persist in a `modal.Volume` (`pep-oracle-whisper-cache`) so cold starts only reseed on first deploy. Cost ~$0.07–0.13 per 2-hour episode; wall-clock ~5–10 min. Deploy with `modal deploy cloud/transcribe_modal.py`. Fail-fast on Modal errors (no fallback). Cache format at `~/.pep-oracle/cache/transcripts/{guid}.whisper.json` is unchanged from the OpenAI-era so pre-existing caches still load.
- **Embedding batches of 20** to stay within OpenAI's 40k TPM rate limit on lower-tier plans.
- **Episode number regex** handles both English `(Ep NNN)` and Spanish `(Episodio NNN)` title formats.
- **`pydub` is a vestigial dependency** — listed in `pyproject.toml` but never imported.
- **`rich` is an unlisted dependency** — used in `cli.py` for Markdown rendering but not declared in `pyproject.toml` (pulled in transitively).
- **Web UI hot reload**: `index.html` is served via `FileResponse` (changes visible on page refresh), but `server.py` changes require a server restart since Python modules are cached at import time.
- **Three ingestion entry points**: CLI (`cli.py ingest`), web API (`server.py POST /ingest`), and systemd timer (`deploy/pep-oracle-ingest.service`). When adding parameters to ingestion, all three must be updated.
- **Speaker diarization**: Optional via CLI (`--diarize` flag), defaults to `True` in the server API. Runs on a Modal GPU — requires `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` env vars. Speaker profiles stored at `~/.pep-oracle/speaker_profiles.json`.
- **Speaker metadata**: Diarized chunks store boolean `has_speaker_chas`, `has_speaker_dave` etc. fields in ChromaDB metadata (replacing the old `speaker_list` comma string). These enable ChromaDB `where` clause filtering by speaker. The `speakers` field (JSON string of turn boundaries) is kept for hybrid trim at query time.
- **Cloud diarization**: Speaker diarization runs on a Modal L4 GPU (`cloud/diarize_modal.py`). Modal downloads the audio from the RSS enclosure URL directly — no local audio needed for diarization. Pyannote weights persist in a `modal.Volume` (`pep-oracle-pyannote-cache`, mounted at `/cache/hf` via `HF_HOME`) so cold starts only reseed on first deploy. Diarization cost is ~$0.05 per 2-hour episode and takes ~5 minutes. Deploy with `modal deploy cloud/diarize_modal.py` after changes. See `cloud/README.md` for one-time setup.
- **RSS feed timeout**: `feed.py` uses `requests.get()` with a 15s timeout for HTTP URLs. The server's `/status` endpoint catches feed failures gracefully so the web UI still loads.

## Environment

Required in `.env` (loaded via python-dotenv):
- `OPENAI_API_KEY` — for embeddings (`text-embedding-3-small`)
- `ANTHROPIC_API_KEY` — for Claude query responses
- `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` — Modal credentials for cloud transcription and diarization

Optional:
- `PEP_ORACLE_DATA_DIR` — override default `~/.pep-oracle/` data directory
- `PEP_ORACLE_HOST` / `PEP_ORACLE_PORT` — server bind address (default `0.0.0.0:8000`)

No host-side ffmpeg required — both Modal images apt-install their own.

## Deployment

`deploy/` contains systemd units for running on a server:
- `pep-oracle-api.service` — runs the FastAPI web server
- `pep-oracle-ingest.service` + `pep-oracle-ingest.timer` — periodic ingestion of new episodes

## Testing

Tests use fixtures in `tests/fixtures/` (RSS XML). External APIs are mocked, including Modal — `pep_oracle.transcripts.whisper.modal` / `pep_oracle.transcripts.diarize.modal` are monkeypatched with a fake whose `Function.from_name(...).remote(...)` returns fixture dicts. ChromaDB tests use ephemeral in-memory clients. Web UI tests (`test_web_*.py`) use Playwright.

Tests marked `@pytest.mark.live` (in `test_web_live.py`) hit real APIs and are excluded by default. Run with `pytest -m live` to include them.

**ChromaDB test isolation**: `chromadb.Client()` (ephemeral) shares state via `SharedSystemClient` cache within a process. Tests that ingest data into a collection must delete it in teardown (`client.delete_collection("pep_oracle")`) or subsequent test files will see stale data.

**Server restart after commits**: A Claude Code `PostToolUse` hook (`.claude/hooks/restart-server.sh`) runs `sudo systemctl restart pep-oracle-api.service` after `git commit` commands so code changes are picked up. The server runs under systemd (`pep-oracle-api.service`); logs go to journald (`journalctl -u pep-oracle-api.service`).

## Future enhancements

- **Improve diarization accuracy**: pyannote segment alignment with Whisper segments can misattribute short segments near turn boundaries; speaker name mapping by speaking time is fragile. Investigate embedding-based speaker matching and tighter alignment heuristics.
- **Speaker-aware chunk boundaries**: Split chunks at speaker turns instead of only at time windows + pauses, so each chunk is dominated by a single speaker. Currently mitigated by the hybrid trim (filtering speaker turns within 4-min chunks at query time).

## Hooks

A Claude Code `PreToolUse` hook runs before `git commit` and blocks unless:
1. `pytest -x -q` passes
2. `/claude-md-improver` has been run and `.claude/.md-reviewed` touched

Stage CLAUDE.md changes before committing so they're included in the same commit.
