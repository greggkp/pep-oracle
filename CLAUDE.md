# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

pep-oracle is an **MCP server** (plus a Fargate ingestion job) that answers US-politics questions over the "PEP with Chas and Dr Dave" podcast using RAG. Episodes are transcribed (faster-whisper on Modal) + diarized, chunked, embedded (AWS Bedrock Titan), and published as a versioned corpus artifact in S3. A frontier model calls the single MCP tool to retrieve grounded excerpts. Deployed AWS-only (serving Lambda + Fargate ingestion); there is no longer a user-facing CLI query path or web GUI.

## Commands

```bash
# Setup (uv manages the venv — no activate needed)
uv pip install -e .
uv pip install -e ".[server]"          # + fastapi/uvicorn/mcp/pyjwt[crypto] for the MCP server

# Tests (uv run picks up the project venv automatically)
uv run pytest
uv run pytest tests/test_feed.py
uv run pytest tests/test_feed.py::test_parse_duration_hhmmss

# Incremental artifact ingest: publish a new corpus version with new feed episodes
uv run pep-oracle ingest-artifact                 # newest-forward (numbered eps > corpus max)
uv run pep-oracle ingest-artifact --backfill      # supervised catch-up of old gaps + EXTRAs

# Retrieval-quality score (recall@k, MRR) over a corpus artifact
uv run pep-oracle eval-retrieval --corpus <s3://… | local-path>

# Local MCP server (dev; prod runs the same app as a Lambda)
uv run pep-oracle-server                          # FastAPI on 0.0.0.0:8000
```

## Architecture

`server.py` (FastAPI) mounts the MCP server and exposes only `/health` + `/version`; `server.handler` is the Mangum Lambda adapter. The two pipelines are ingestion (Fargate `ingest-artifact`) and retrieval (the MCP tool).

**Ingestion** (`ingest_artifact.py` orchestrates):
load current S3 corpus → find new feed episodes (`feed.py` RSS parse) → per episode `ingest.episode_chunks_and_embeddings`: **concurrent** [`transcripts/manager.py` (Whisper via `cloud/transcribe_modal.py`) ‖ `transcripts/diarize.py` `get_speaker_segments` (pyannote via `cloud/diarize_modal.py`)] → `apply_diarization` (aligns speakers + maps host names) → `chunking.py` (time-window chunks with overlap) → `embeddings.py` (Bedrock Titan) → corpus rows (`store._chunk_metadata`) → merge + publish `vN+1` (atomic `current.json` flip). The serving Lambda TTL-refreshes within about 5 min. No ChromaDB anywhere.

**Hybrid retrieval** (`hybrid.py` + `lexical.py`): semantic embeddings blur distinctive terms (proper nouns, bill names) that a politics podcast is full of; BM25 (`lexical.BM25`, with digit→word `normalize_numbers` so "day 2"≈"day two") nails those but is blind to paraphrase. `hybrid_search` ranks candidates by both and merges with **weighted Reciprocal Rank Fusion** (`SEMANTIC_WEIGHT=0.8`, tuned via the eval harness — leans semantic so BM25 rescues distinctive-term queries without diluting topic queries), returning query-shaped dicts with a rank-based `distance` so the temporal layer treats the fused rank as relevance. The BM25 index + corpus is cached per `(collection name, corpus version)` (with a chunk-count staleness check) and rebuilt on each corpus version swap; exhaustive local ranking is fine at this corpus size (≤about 10k chunks).

**Temporal reranking** (`temporal.py`): recency must be *intent-gated*, not global — a blanket recency prior makes old-but-relevant content unretrievable (the "temporal event horizon"). `intent` ∈ {`current`, `historical`, `evolution`, `prediction`, `timeless`}: `current` → exponential recency decay (`HALF_LIFE_DAYS`), newest-first, NO hard date cut; `evolution` → spread across episodes, chronological; `prediction` → relevance, chronological (so the reader sees prediction→outcome); `historical`/`timeless` → pure relevance, newest-first.

**MCP server** (`mcp_server.py` + `oauth.py`) — the core product:
Exposes a single tool (Python `search_pep`, exported as `search_us_politics_commentary`; `search_pep(query, top_k=5, episode_number=None, intent=None, after_date=None, before_date=None)`) over the official `mcp` SDK's Streamable HTTP transport. It embeds the query (Bedrock) → `hybrid_search` → `temporal.select_for_intent`, returning `{"corpus": {newest/oldest episode}, "results": [citation dicts: episode number, title, date, timestamp, speakers, excerpt]}`. Default ranking is relevance, so the *caller* (a frontier model) scopes via `episode_number` (use `corpus.newest_episode` for "the latest episode") and shapes recency via `intent`; `evolution`/`prediction` return oldest-first; `after_date`/`before_date` hard-filter a window. The retrieval source is always the corpus artifact via `get_serving_corpus` → `corpus.current_corpus` (TTL-refreshed `InMemoryCorpus`, atomic version swap). Mounted at `/mcp` by `server.mount_mcp_if_configured()`, gated by JWT bearer verification against an in-app OAuth 2.1 + DCR provider (`oauth.py`, HS256 access tokens, 60s auth codes, 30d rotating refresh tokens with family revocation on reuse). Discovery doc at `/.well-known/oauth-authorization-server`; routes under `/oauth/{register,authorize,token,revoke}`. The signing key is resolved via pluggable `signing.py` (`local` env→file→generate, or `ssm` SecureString); `/oauth/authorize` is gated by pluggable `authorize_gate.py` (`TrustedUpstreamGate` default; `CognitoGate` brokers a Cognito Hosted-UI login → `/oauth/authorize/callback` verifies the ID token via the pool JWKS + email allow-list, carrying the MCP params in a stateless HS256 login-state JWT so PKCE survives the round-trip). Mount requires `PEP_ORACLE_PUBLIC_URL` plus either `PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH=1` or `PEP_ORACLE_AUTHORIZE_GATE=cognito`; refuses to start otherwise so `/oauth/authorize` can't accidentally be exposed open. Serves stateless (`FastMCP(..., stateless_http=True)`) so any Lambda container handles any request.

## Key design decisions

- **Corpus artifact** (`corpus.py`): a versioned immutable parquet (`<CORPUS_URI>/corpus/vNNNN.parquet` + `.manifest.json` + mutable `current.json`) re-loadable into an `InMemoryCorpus` that is a drop-in for `hybrid_search`/`store.get_ingestion_stats`. It is the **only** serving source (ChromaDB was removed). `corpus.current_corpus()` is the TTL-refreshed, version-swapped serving accessor (`CORPUS_REFRESH_TTL_SECONDS`, default 300s); `corpus._validate_serving` enforces dims + embedder against the manifest at load. `GET /version` reports code (semver + `GIT_SHA`) and corpus (version, episode_range, built_at, embed_model).
- **Bedrock-only embeddings** (`embeddings.py`): `embed_texts()` calls AWS Bedrock Titan (`amazon.titan-embed-text-v2:0`, 1024-dim, region `BEDROCK_REGION`), one `InvokeModel` per text with throttle backoff. There is no local/fastembed path. Query and corpus vectors must come from the same model — the corpus manifest records `embed_model`/`EMBED_MODEL` and the loader validates it.
- **Incremental ingestion**: episodes tracked by GUID + episode number in corpus metadata; already-present episodes are skipped. **Selection is newest-forward by default** (only numbered episodes *newer* than the corpus max) so one permanent back-catalogue gap (eps 179–216 were never transcribed; unnumbered "EXTRA" bonus episodes) can't turn every daily run into a fragile all-or-nothing job — publish is one `current.json` flip after the whole loop. `--backfill` (operator-run, supervised) ingests every feed episode the corpus lacks.
- **Cloud transcription**: Modal A100 GPU (`cloud/transcribe_modal.py`), `faster-whisper large-v3-turbo`. Modal fetches audio from the RSS enclosure URL — no local audio download. Weights persist in a `modal.Volume` (`pep-oracle-whisper-cache`). About 1 min per 2-hour episode; runs concurrently with diarization. Deploy with `modal deploy cloud/transcribe_modal.py`. Fail-fast (no fallback). Transcript cache at `~/.pep-oracle/cache/transcripts/{guid}.whisper.json`.
- **Cloud diarization**: Modal A100 GPU (`cloud/diarize_modal.py`), pyannote. Audio fetched from the RSS enclosure URL. Weights persist in a `modal.Volume` (`pep-oracle-pyannote-cache`, mounted at `/cache/hf` via `HF_HOME`). About 2–3 min per 2-hour episode, concurrent with transcription. Deploy with `modal deploy cloud/diarize_modal.py`. See `cloud/README.md`. Diarization cache is v2 `{segments, clusters}`; loader is back-compatible with old bare-list caches.
- **Speaker name mapping** (`transcripts/diarize.py`): pyannote over-segments this audio and can't cluster Chas vs Dave, but per-cluster *embeddings* separate hosts cleanly. `map_speaker_names` matches each cluster to reference voices in `speaker_profiles.json` via `assign_by_voice` (`VOICE_MATCH_MAX_DISTANCE=0.5`), collapsing a host's over-split fragments and excluding guests; fallback `assign_substantive_speakers` uses speaking-time when no references exist. Do NOT cap `max_speakers` (it merges the hosts). The Fargate ingest reads references from `s3://…/refs/speaker_profiles.json` (`SPEAKER_PROFILES_URI`); the local file lives at `~/.pep-oracle/speaker_profiles.json`.
- **Speaker metadata**: diarized chunks store boolean `has_speaker_chas`, `has_speaker_dave`, etc. (mapped host names, never raw `speaker_N`) plus a `speakers` JSON string of turn boundaries for hybrid speaker trimming at query time (`store._chunk_metadata`).
- **OAuth store seam** (`oauth_store.py`): clients + single-use auth codes + refresh tokens behind `OAuthStore`; `PEP_ORACLE_OAUTH_STORE` selects `sqlite` (local default, `~/.pep-oracle/oauth.db`) or `dynamodb` (Lambda). Rotation is race-safe via a conditional `revoke_refresh` (won/lost) — concurrent refreshes → exactly one rotation, loser gets a clean 400, no spurious family revoke; genuine reuse revokes the family. DynamoDB = single table + `family-index` GSI + native TTL; `DynamoDbStore.ensure_table()` is local/moto only (prod table from CDK). Contract tests (`tests/test_oauth_store.py`, moto) run every behavior against both backends.
- **MCP tool name + description are load-bearing and must be front-loaded**: MCP clients (iOS Claude, Claude.ai) *defer* tools — they see only the tool name and a *truncated* description until a tool-search loads the full schema, so trigger language in a trailing paragraph never influences whether the tool gets called. So in `mcp_server.py`: (1) the tool is exported under the explicit descriptive name `search_us_politics_commentary` (`SEARCH_TOOL_NAME`), not the opaque `search_pep`, because the name always survives truncation; (2) `SEARCH_PEP_DESCRIPTION` leads with the "when to call" trigger and puts the "it's a podcast" framing last. If you edit either, keep the trigger in sentence one and re-test a positive case (US-politics question / news-article explainer) AND a negative case (recipe, JS bug). Client-side retrieval can't be forced — good wording only raises the odds.
- **`/oauth/authorize` is gated at the edge, not in app** (default `trusted_upstream` gate): the handler auto-approves any well-formed request, so the deployment MUST sit behind an upstream authenticator restricting who can reach the route (e.g. a Cloudflare Access Self-hosted app scoped to `/oauth/authorize`). `/oauth/register`, `/oauth/token`, `/.well-known/...`, and `/mcp` must stay open at the edge — they're server-to-server or PKCE/JWT-protected. `PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH=1` is a fail-closed switch asserting the operator wired up the upstream gate; the app refuses to mount OAuth routes without it. Alternatively `PEP_ORACLE_AUTHORIZE_GATE=cognito` is an in-app identity gate that removes the external-edge dependency.
- **Episode number regex** handles both English `(Ep NNN)` and Spanish `(Episodio NNN)` title formats.
- **Data dir**: local state lives at `~/.pep-oracle/` (transcript/diarization caches, `speaker_profiles.json`, `oauth.db`); override with `PEP_ORACLE_DATA_DIR`. On Lambda (read-only FS) `DATA_DIR=/tmp` and the corpus comes from `CORPUS_URI` (S3).

## Environment

Required in `.env` (loaded via python-dotenv):
- `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` — Modal credentials for cloud transcription and diarization

Optional:
- `PEP_ORACLE_DATA_DIR` — override default `~/.pep-oracle/` data directory
- `PEP_ORACLE_CORPUS_URI` — base URI of the corpus artifact (S3 in prod; defaults to the data dir)
- `PEP_ORACLE_HOST` / `PEP_ORACLE_PORT` — local server bind address (default `0.0.0.0:8000`)
- `PEP_ORACLE_PUBLIC_URL` — public issuer URL in the OAuth discovery doc; must match the tunnel hostname (e.g. `https://pep-oracle.iicapn.com`). Required to enable `/mcp`.
- `PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH` — literal `1` to mount `/oauth/*` and `/mcp`. Asserts an upstream gate protects `/oauth/authorize`. Any other value → mount skipped with ERROR log.
- `PEP_ORACLE_OAUTH_SIGNING_KEY` — HS256 signing key for access-token JWTs. If unset, falls back to `~/.pep-oracle/oauth_signing_key` (mode 0600); auto-generated on first start if absent.
- `PEP_ORACLE_OAUTH_SIGNING_BACKEND` — `local` (default: env/file/generate) or `ssm` (HS256 SecureString from `PEP_ORACLE_OAUTH_SIGNING_SSM_PARAM`, region `PEP_ORACLE_OAUTH_SIGNING_SSM_REGION`). Lambda uses `ssm`; missing/empty param fails closed.
- `PEP_ORACLE_AUTHORIZE_GATE` — `trusted_upstream` (default) or `cognito`. `cognito` enables the in-app one-user Cognito identity gate and does NOT require `TRUSTS_UPSTREAM_AUTH=1`; needs `PEP_ORACLE_COGNITO_{DOMAIN,CLIENT_ID,CLIENT_SECRET,USER_POOL_ID,REGION,ALLOWED_EMAILS}` (see `docs/aws/phase2b2-signing-and-cognito.md`).

No host-side ffmpeg required — both Modal images apt-install their own.

## Deployment

The product runs entirely on AWS — the OptiPlex serving/ingest/DNS-rollback fallback was decommissioned 2026-06-09. `infra/` is a CDK Python app (isolated `infra/.venv`, excluded from root pytest via `--ignore=infra`, tested with `cd infra && .venv/bin/python -m pytest`):
- **Serving (Phase 2c)**: `PepOracleCertStack` (us-east-1: Route 53 zone + ACM cert) + `PepOracleProdStack` (ap-southeast-2: KMS, S3 corpus bucket, DynamoDB OAuth table matching `oauth_store.DynamoDbStore`, one-user Cognito pool, container Lambda `pep_oracle.server.handler` fronted by CloudFront → **API Gateway HTTP API** ($default proxy), least-privilege IAM). Lambda-readiness: per-request stateless MCP session manager (Mangum runs the ASGI lifespan per-invoke), `DATA_DIR=/tmp`, disabled MCP DNS-rebinding host-check + `/mcp`→`/mcp/` normalizer (behind CloudFront→APIGW the Lambda sees the execute-api Host). Account facts: Lambda concurrency=10; public Function URLs blocked account-wide (hence APIGW). See `docs/aws/phase2c-{lambda-compat-findings,deploy-runbook}.md`. `pep-oracle.iicapn.com` is on AWS (Route 53 NS delegation at Cloudflare).
- **Ingestion (Phase 3, `infra/ingest_stack.py`)**: daily EventBridge rule → scale-to-zero Fargate task running `pep-oracle ingest-artifact`. Modal tokens via SSM SecureString. The ingest stack imports the corpus bucket + KMS key as **external** resources (bucket by name, key ARN from `cfg.data_key_id`) so deploying it never redeploys the serving Lambda. Deploy/decommission: `docs/aws/phase3-ingestion-runbook.md`.
- **CI/CD (Phase 4, `.github/workflows/`)**: `ci.yml` gates every PR + push to main (`ruff check` + root `pytest` + infra `pytest` + `cdk synth '*'` + docker build of both images, no AWS access). `deploy.yml` runs on a `v*` tag (or `workflow_dispatch` on a tag, for rollback): assumes the GitHub-OIDC deploy role from `PepOracleCicdStack`, runs `cdk deploy PepOracleProdStack PepOracleIngestStack` (deploy context via `env:`, `-c git_sha -c semver`), then `scripts/smoke.py` (auth-free: `/health`, `/version` sha+semver match, `/.well-known/...`, `/mcp` no-token→401, retries for cold start). The release tag surfaces as `GET /version` `code_semver` (`config.SEMVER` overrides the package version). Rollback = re-run `deploy` on the prior `v*` tag (image already in ECR). One-time bootstrap: `cdk deploy PepOracleCicdStack` with admin creds + set repo variables `AWS_DEPLOY_ROLE_ARN` + `ALLOWED_EMAIL`. `PepOracleCertStack` stays a manual deploy. Runbook `docs/aws/phase4-cicd-runbook.md`.

## Testing

Tests use fixtures in `tests/fixtures/` (RSS XML). External APIs are mocked, including Modal — `pep_oracle.transcripts.whisper.modal` / `pep_oracle.transcripts.diarize.modal` are monkeypatched with a fake whose `Function.from_name(...).remote(...)` returns fixture dicts.

Tests marked `@pytest.mark.live` hit real APIs/data and are excluded by default (`pytest -m live` to include) — they exercise the long-lived server + real corpus where integration bugs actually live:
- `test_smoke_live.py` — hits the running server; `/mcp` rejects no-token (401) and accepts a minted JWT without a 421. Override target via `PEP_ORACLE_SMOKE_URL`.
- `test_data_integrity_live.py` — reads metadata from the current corpus artifact (`config.CORPUS_URI`, the same `InMemoryCorpus` the MCP tool serves) and asserts diarized episodes expose mapped `has_speaker_chas`/`has_speaker_dave`, not raw `has_speaker_speaker_N`.
- `test_eval_retrieval_live.py` — retrieval-quality regression guard (hybrid recall@10/MRR over the real corpus). Quality is measured by `eval_retrieval.py` (phrase-grounded, type-tagged query set; `pep-oracle eval-retrieval` prints recall@k/MRR).

## Hooks

- A `PreToolUse` hook (`.claude/hooks/pre-commit.sh`, committed via `.claude/settings.json`) runs before `git commit` and blocks unless (1) `pytest -x -q` passes and (2) `/claude-md-improver` has been run with `.claude/.md-reviewed` touched newer than `CLAUDE.md`. Stage CLAUDE.md changes before committing so they land in the same commit.

## Future enhancements

- **Speaker-aware chunk boundaries**: split chunks at speaker turns instead of only at time windows + pauses, so each chunk is dominated by a single speaker. Currently mitigated by the hybrid trim (filtering speaker turns within 4-min chunks at query time).
