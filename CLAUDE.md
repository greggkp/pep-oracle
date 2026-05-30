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
`feed.py` (RSS parse) → **concurrent**: [`transcripts/manager.py` (Whisper via `cloud/transcribe_modal.py`) ‖ `transcripts/diarize.py` `get_speaker_segments` (pyannote via `cloud/diarize_modal.py`)] → `apply_diarization` (aligns speakers to transcript) → `chunking.py` (time-window chunks with overlap) → `embeddings.py` (local fastembed / bge-large) → `store.py` (ChromaDB upsert). Web API ingestion runs in a subprocess (`ingest_worker.py`) to isolate ingest failures from the server process.

**Query** (`query.py` orchestrates):
Pre-process question via Claude Haiku (extract date/episode/speaker filters + recency intent) → embed search query (local fastembed) → retrieve top-k chunks from ChromaDB (with filters + optional recency re-ranking + optional speaker filtering) → trim chunks to target speaker's portions if speaker filter active (`_trim_to_speaker`) → build prompt with transcript excerpts sorted newest-first → send to Claude → render with rich Markdown. Compare queries ("Chas vs Dave on X") run dual retrieval (one per speaker, `top_k/2` each) with labeled context sections.

**Topic chips** (`topics.py`):
Topics are extracted deterministically from episode show notes at ingestion time: `parse_description_topics()` extracts timestamp labels → `clean_episode_topics()` strips segment prefixes (Correspondence, Not Normal, Stats Nug, Policy Time), extracts parenthetical subtopics, cleans Unleashed entries, and strips Cont. suffixes → `_ingest_one()` returns the topic entry; callers (`ingest_all`, `ingest_episode`) batch entries and call `save_topics()` once → persisted to `~/.pep-oracle/topics.json`. `/topics` endpoint reads from file — no API call. Frontend renders chips grouped by episode with inline episode numbers ("Cuba · Ep 253"), "More..." button loads older episodes.

**MCP server** (`mcp_server.py` + `oauth.py`):
Exposes a single tool (Python `search_pep`, exported as `search_us_politics_commentary`; `top_k=5`) over the official `mcp` Python SDK's Streamable HTTP transport. The tool reuses `embeddings.py` + `store.py` retrieval primitives (no Haiku pre-processor, no internal Claude call) and returns `{"corpus": {newest/oldest episode}, "results": [citation dicts: episode number, title, date, timestamp, speakers, excerpt]}`. The `corpus` summary lets the caller answer "latest episode" questions since `results` are relevance-ranked, not recency-ranked. Mounted at `/mcp` by `server.py:mount_mcp_if_configured()`, gated by JWT bearer verification against an in-app OAuth 2.1 + DCR provider (`oauth.py`, SQLite store at `~/.pep-oracle/oauth.db`, HS256 access tokens, 60s auth codes, 30d rotating refresh tokens with family revocation on reuse). Discovery doc at `/.well-known/oauth-authorization-server`; routes under `/oauth/{register,authorize,token,revoke}`. Mount requires `PEP_ORACLE_PUBLIC_URL` and `PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH=1` — refuses to start otherwise so `/oauth/authorize` can't accidentally be exposed open.

## Key design decisions

- **Data transfer between machines**: Use `pep-oracle export` / `import` to move ingested episodes. Never copy ChromaDB files directly — ChromaDB must handle its own writes via upsert to avoid corruption. Export produces a JSON file with chunks, embeddings, and metadata.
- **Data stored at `~/.pep-oracle/`** (cache/transcripts, cache/diarization, chroma), not in the project directory. Override with `PEP_ORACLE_DATA_DIR`. (`cache/audio/` is no longer written — both Modal functions fetch audio directly from `episode.audio_url`. The directory is left alone if it exists from a prior install; `rm -rf` at will.)
- **Incremental ingestion**: episodes tracked by GUID in ChromaDB metadata; already-ingested episodes are skipped unless `--force`.
- **Cloud transcription**: Transcription runs on a Modal A100 GPU (`cloud/transcribe_modal.py`) using `faster-whisper large-v3-turbo`. Modal fetches audio from the RSS enclosure URL — no local audio download. Model weights persist in a `modal.Volume` (`pep-oracle-whisper-cache`) so cold starts only reseed on first deploy. Wall-clock ~1 min per 2-hour episode; runs concurrently with diarization from `ingest.py`. Deploy with `modal deploy cloud/transcribe_modal.py`. Fail-fast on Modal errors (no fallback). Cache format at `~/.pep-oracle/cache/transcripts/{guid}.whisper.json` is unchanged.
- **Local embeddings**: `embeddings.py` loads `BAAI/bge-large-en-v1.5` via `fastembed` as a lazy singleton. First use downloads ≈1.3 GB of ONNX weights to `~/.cache/fastembed/`; subsequent loads are ≈5s (cold process) or free (warm). Output is 1024-dim; any migration that changes the embedding model must drop-and-recreate the Chroma collection to match.
- **Episode number regex** handles both English `(Ep NNN)` and Spanish `(Episodio NNN)` title formats.
- **`pydub` is a vestigial dependency** — listed in `pyproject.toml` but never imported.
- **`rich` is an unlisted dependency** — used in `cli.py` for Markdown rendering but not declared in `pyproject.toml` (pulled in transitively).
- **Web UI hot reload**: `index.html` is served via `FileResponse` (changes visible on page refresh), but `server.py` changes require a server restart since Python modules are cached at import time.
- **Three ingestion entry points**: CLI (`cli.py ingest`), web API (`server.py POST /ingest`), and systemd timer (`deploy/pep-oracle-ingest.service`). When adding parameters to ingestion, all three must be updated.
- **Speaker diarization**: Optional via CLI (`--diarize` flag), defaults to `True` in the server API. Runs on a Modal GPU — requires `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` env vars. Speaker profiles stored at `~/.pep-oracle/speaker_profiles.json`.
- **Speaker metadata**: Diarized chunks store boolean `has_speaker_chas`, `has_speaker_dave` etc. fields in ChromaDB metadata (replacing the old `speaker_list` comma string). These enable ChromaDB `where` clause filtering by speaker. The `speakers` field (JSON string of turn boundaries) is kept for hybrid trim at query time.
- **Cloud diarization**: Speaker diarization runs on a Modal A100 GPU (`cloud/diarize_modal.py`). Modal downloads the audio from the RSS enclosure URL directly — no local audio needed for diarization. Pyannote weights persist in a `modal.Volume` (`pep-oracle-pyannote-cache`, mounted at `/cache/hf` via `HF_HOME`) so cold starts only reseed on first deploy. Diarization takes ~2–3 min per 2-hour episode and runs concurrently with transcription. Deploy with `modal deploy cloud/diarize_modal.py` after changes. See `cloud/README.md` for one-time setup.
- **RSS feed timeout**: `feed.py` uses `requests.get()` with a 15s timeout for HTTP URLs. The server's `/status` endpoint catches feed failures gracefully so the web UI still loads.
- **MCP tool name + description are load-bearing and must be front-loaded**: MCP clients (iOS Claude, Claude.ai) *defer* tools — they see only the tool name and a *truncated* description until a tool-search loads the full schema, so trigger language in a trailing paragraph never influences whether the tool gets called. Two consequences encoded in `mcp_server.py`: (1) the tool is exported under the explicit descriptive name `search_us_politics_commentary` (`SEARCH_TOOL_NAME`), not the opaque Python function name `search_pep`, because the name always survives truncation; (2) `SEARCH_PEP_DESCRIPTION` leads with the "when to call" trigger and puts the "it's a podcast" framing last. If you edit either, keep the trigger in sentence one and re-test a positive case (US-politics question / news-article explainer) AND a negative case (recipe, JS bug) to confirm call frequency didn't shift. Note: client-side retrieval can't be forced — good wording only raises the odds.
- **`/oauth/authorize` is gated at the edge, not in app**: the handler auto-approves any well-formed request, so the deployment MUST sit behind an upstream authenticator that restricts who can reach the route. The recommended setup is a Cloudflare Access Self-hosted app scoped to the path `/oauth/authorize` (One-time PIN policy is enough for a single-user box). `/oauth/register`, `/oauth/token`, `/.well-known/...`, and `/mcp` must stay open at the edge — they're server-to-server or PKCE/JWT-protected. The `PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH=1` env var is a fail-closed switch confirming the operator wired up the upstream gate; the app refuses to mount OAuth routes without it.

## Environment

Required in `.env` (loaded via python-dotenv):
- `ANTHROPIC_API_KEY` — for Claude query responses
- `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` — Modal credentials for cloud transcription and diarization

No `OPENAI_API_KEY` — embeddings are now generated locally via `fastembed`.

Optional:
- `PEP_ORACLE_DATA_DIR` — override default `~/.pep-oracle/` data directory
- `PEP_ORACLE_HOST` / `PEP_ORACLE_PORT` — server bind address (default `0.0.0.0:8000`)
- `PEP_ORACLE_PUBLIC_URL` — public issuer URL used in the OAuth discovery doc; must match the tunnel hostname (e.g. `https://pep-oracle.iicapn.com`). Required to enable `/mcp`.
- `PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH` — must be the literal string `1` to mount `/oauth/*` and `/mcp`. Asserts that an upstream gate (e.g. Cloudflare Access) protects `/oauth/authorize`. Any other value (including absent) → mount skipped with ERROR log.
- `PEP_ORACLE_OAUTH_SIGNING_KEY` — HS256 signing key for access-token JWTs. If unset, falls back to `~/.pep-oracle/oauth_signing_key` (file mode 0600); if that file doesn't exist, one is auto-generated on first start.

No host-side ffmpeg required — both Modal images apt-install their own.

## Deployment

`deploy/` contains systemd units for running on a server:
- `pep-oracle-api.service` — runs the FastAPI web server
- `pep-oracle-ingest.service` + `pep-oracle-ingest.timer` — periodic ingestion of new episodes

## Testing

Tests use fixtures in `tests/fixtures/` (RSS XML). External APIs are mocked, including Modal — `pep_oracle.transcripts.whisper.modal` / `pep_oracle.transcripts.diarize.modal` are monkeypatched with a fake whose `Function.from_name(...).remote(...)` returns fixture dicts. ChromaDB tests use ephemeral in-memory clients. Web UI tests (`test_web_*.py`) use Playwright.

Tests marked `@pytest.mark.live` hit real APIs/data and are excluded by default (`pytest -m live` to include). They exist because unit tests mock the long-lived server + real corpus where integration bugs actually live: `test_web_live.py` (UI vs DB), `test_smoke_live.py` (hits the running server — `/ask` answers aren't dead-ends, `/episodes` is current, `/mcp` rejects no-token and accepts a minted JWT without 421; override target via `PEP_ORACLE_SMOKE_URL`), `test_data_integrity_live.py` (asserts diarized episodes expose mapped `has_speaker_chas`/`has_speaker_dave`, not raw `speaker_N`).

**ChromaDB test isolation**: `chromadb.Client()` (ephemeral) shares state via `SharedSystemClient` cache within a process. Tests that ingest data into a collection must delete it in teardown (`client.delete_collection("pep_oracle")`) or subsequent test files will see stale data.

**Server restart after commits**: A Claude Code `PostToolUse` hook (`.claude/hooks/restart-server.sh`) runs `sudo systemctl restart pep-oracle-api.service` after `git commit` commands so code changes are picked up. The server runs under systemd (`pep-oracle-api.service`); logs go to journald (`journalctl -u pep-oracle-api.service`).

## Future enhancements

- **Speaker-name mapping (voice-embedding ID)**: pyannote over-segments this audio (16–30 micro-clusters/episode) and can't cluster Chas vs Dave — but its per-cluster *embeddings* separate them cleanly (same host across episodes ≤0.026 cosine, Chas↔Dave ≥0.85, guests ≥0.9). So `diarize.diarize` returns per-cluster embeddings (`return_embeddings`) + `intro_seconds`; `diarize.assign_by_voice` matches each cluster to reference voices (`VOICE_MATCH_MAX_DISTANCE=0.5`) — collapsing a host's over-split fragments back into them, and excluding guests (non-match + substantive → Guest, else skip). Do NOT cap `max_speakers` (it merges the hosts). References are auto-built by `pep-oracle build-references` (`references.py`): **Chas = the intro speaker** (he always opens the show — max `intro_seconds`), **Dave = the substantive 2nd cluster on Dr-Dave-titled episodes**; stored as real embeddings in `speaker_profiles.json`. Fallback when no references/embeddings: `assign_substantive_speakers` (speaking-time, top clusters→Chas/Dave/Guest, tail skipped). Re-process existing data with `pep-oracle remap-speakers` (rebuilds chunks from cached transcript+diarization, reuses stored embeddings by `chunk_id` — no re-embed; reconstructs out-of-feed episodes from metadata). Diarization cache is v2 `{segments, clusters}`; loader is back-compatible with the old bare-list caches (`clusters=None`).
- **Speaker-aware chunk boundaries**: Split chunks at speaker turns instead of only at time windows + pauses, so each chunk is dominated by a single speaker. Currently mitigated by the hybrid trim (filtering speaker turns within 4-min chunks at query time).

## Hooks

A Claude Code `PreToolUse` hook runs before `git commit` and blocks unless:
1. `pytest -x -q` passes
2. `/claude-md-improver` has been run and `.claude/.md-reviewed` touched

Stage CLAUDE.md changes before committing so they're included in the same commit.
