# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

pep-oracle is a CLI tool that transcribes and queries the "PEP with Chas and Dr Dave" podcast using RAG. It ingests episodes (via OpenAI Whisper), chunks and embeds them, stores in ChromaDB, and answers natural language questions via Claude.

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
`feed.py` (RSS parse) → `transcripts/manager.py` (Whisper with caching) → `chunking.py` (time-window chunks with overlap) → `embeddings.py` (OpenAI batched) → `store.py` (ChromaDB upsert)

**Query** (`query.py` orchestrates):
Pre-process question via Claude Haiku (extract date/episode filters + recency intent) → embed search query (OpenAI) → retrieve top-k chunks from ChromaDB (with filters + optional recency re-ranking) → build prompt with transcript excerpts sorted newest-first → send to Claude → render with rich Markdown

## Key design decisions

- **Data transfer between machines**: Use `pep-oracle export` / `import` to move ingested episodes. Never copy ChromaDB files directly — ChromaDB must handle its own writes via upsert to avoid corruption. Export produces a JSON file with chunks, embeddings, and metadata.
- **Audio splitting uses ffmpeg directly** (not pydub) for speed — seeks without decoding the full file. Requires ffmpeg on PATH.
- **Data stored at `~/.pep-oracle/`** (cache/audio, cache/transcripts, chroma), not in the project directory. Override with `PEP_ORACLE_DATA_DIR`.
- **Incremental ingestion**: episodes tracked by GUID in ChromaDB metadata; already-ingested episodes are skipped unless `--force`.
- **Embedding batches of 20** to stay within OpenAI's 40k TPM rate limit on lower-tier plans.
- **Episode number regex** handles both English `(Ep NNN)` and Spanish `(Episodio NNN)` title formats.
- **`pydub` is a vestigial dependency** — listed in `pyproject.toml` but never imported. Audio splitting uses ffmpeg subprocess calls directly.
- **`rich` is an unlisted dependency** — used in `cli.py` for Markdown rendering but not declared in `pyproject.toml` (pulled in transitively).

## Environment

Required in `.env` (loaded via python-dotenv):
- `OPENAI_API_KEY` — for embeddings (`text-embedding-3-small`) and Whisper transcription
- `ANTHROPIC_API_KEY` — for Claude query responses

Optional:
- `PEP_ORACLE_DATA_DIR` — override default `~/.pep-oracle/` data directory
- `PEP_ORACLE_HOST` / `PEP_ORACLE_PORT` — server bind address (default `0.0.0.0:8000`)

Requires **ffmpeg** on PATH for audio splitting.

## Deployment

`deploy/` contains systemd units for running on a server:
- `pep-oracle-api.service` — runs the FastAPI web server
- `pep-oracle-ingest.service` + `pep-oracle-ingest.timer` — periodic ingestion of new episodes

## Testing

Tests use fixtures in `tests/fixtures/` (RSS XML). External APIs are mocked. ChromaDB tests use ephemeral in-memory clients. Whisper splitting tests generate audio via ffmpeg's sine generator. Web UI tests (`test_web_*.py`) use Playwright.

Tests marked `@pytest.mark.live` (in `test_web_live.py`) hit real APIs and are excluded by default. Run with `pytest -m live` to include them.

## Hooks

A Claude Code `PreToolUse` hook runs before `git commit` and blocks unless:
1. `pytest -x -q` passes
2. `/claude-md-improver` has been run and `.claude/.md-reviewed` touched

Stage CLAUDE.md changes before committing so they're included in the same commit.
