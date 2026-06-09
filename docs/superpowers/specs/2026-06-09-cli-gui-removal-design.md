# CLI + GUI removal (AWS-only excision) — design spec

**Date:** 2026-06-09
**Status:** Approved (brainstorm complete; ready for implementation plan)
**Sequencing:** This lands **first**; the portable-dev-env spec
(`2026-06-09-portable-dev-env-design.md`) is reworked against the slimmed tree
**after** this merges.
**Related:** OptiPlex decommission (shares the local-path teardown), AWS MCP
migration (`2026-06-02-aws-mcp-migration-design.md`).

## Goal

Remove the user-facing CLI and the web GUI from pep-oracle, and excise the
now-orphaned local-ChromaDB support code, leaving an **AWS-only** codebase: the
MCP serving Lambda (corpus artifact + Bedrock) and the Fargate ingestion task.
Deploys, ingestion, and serving already run on AWS; the local CLI/GUI are
OptiPlex-era interfaces with no remaining production role.

Scope is **full AWS-only excision** (approved), not a surface-only deletion: the
local-ChromaDB modules that exist only to support the CLI/GUI are removed too, so
no dead local path remains.

## Decisions (approved)

- **Full AWS-only excision** — remove CLI/GUI **and** the orphaned local support
  code (split `store.py`/`ingest.py`, delete `query.py`/`topics.py`/etc., drop the
  ChromaDB live-serving branch).
- **Keep exactly two CLI commands:** `ingest-artifact` (Fargate's entrypoint) and
  `eval-retrieval` (a dev/regression tool, adapted to corpus-artifact-only).
- **Remove the other ops commands:** `build-references`, `remap-speakers`,
  `export`, `backfill`, `backup`, plus the local user commands `episodes`,
  `ingest`, `ask`, `status`, `import`.
- **Drop dependencies:** `fastembed` (→ `embeddings.py` becomes Bedrock-only),
  `chromadb` (no longer imported after the `store.py` split), `pydub` (already
  vestigial).

## Survivor map

### Keep — the AWS product

**Serving (Lambda):** `server.py` (product routes only), `mcp_server.py`,
`oauth.py`, `oauth_store.py`, `signing.py`, `authorize_gate.py`, `corpus.py`,
`hybrid.py`, `lexical.py`, `temporal.py`, `embeddings.py` (Bedrock-only),
`config.py`, `models.py`, `_storage.py`, `cache.py`.

**Ingestion (Fargate):** `cli.py` (group + `ingest-artifact` + `eval-retrieval`),
`ingest_artifact.py`, `ingest.py` (`episode_chunks_and_embeddings` + helpers),
`feed.py`, `chunking.py`, `transcripts/` (`whisper.py`, `diarize.py`,
`manager.py`), `cloud/` (`transcribe_modal.py`, `diarize_modal.py`).

**`store.py` — split** (keep, drops the `chromadb` import entirely):
- Keep: `SENTINEL_NO_TIME` (used by `hybrid`), `get_ingestion_stats` (operates on
  the `InMemoryCorpus`), `_chunk_metadata` (used by `ingest_artifact`).
- Delete: `get_client`, `get_collection`, `get_fresh_collection`, `add_chunks`,
  `query`, `_apply_recency_boost`, `get_ingested_guids`, `delete_episode`,
  `export_episodes`, `import_chunks`, `_build_where` (verify `_build_where` has no
  surviving caller; it backs the removed `store.query`).
- Consider renaming the surviving file to reflect its role (e.g. `corpus_meta.py`).
  Optional; a rename touches imports in `mcp_server`/`server`/`ingest_artifact`.

### Remove — CLI + GUI + orphaned support

**Delete modules:** `query.py` (after extracting `format_timestamp`),
`topics.py`, `references.py`, `remap_speakers.py`, `backfill.py`, `backup.py`,
`ingest_worker.py`, `web/index.html` (and the `web/` dir).

**`ingest.py` — trim:** delete `_ingest_one`, `ingest_all`, `ingest_episode`
(ChromaDB writers) and their `topics` + `store` (ChromaDB) imports; keep
`episode_chunks_and_embeddings` and its helpers.

**`cli.py` — trim:** delete `episodes`, `ingest`, `ask`, `status`, `import`,
`export`, `backfill`, `backup`, `build-references`, `remap-speakers`. Keep the
`cli` group, `ingest-artifact`, `eval-retrieval`.

**`server.py` — trim:** delete GUI routes `/`, `/ask`, `/status`, `/episodes`,
`/topics`, `/ingest`, `/ingest/status` and their helpers (`_get_fresh_collection`
alias, etc.). Keep `/health`, `/version`, `/freshness`, the MCP mount, the OAuth
routes, and the Mangum `handler`. Drop the `FileResponse`/`WEB_DIR` import.

**`mcp_server.py` — decouple:** drop `from pep_oracle.store import
get_fresh_collection` and the live-ChromaDB branch in `get_serving_corpus` (it
always returns the artifact corpus now). Keep `get_ingestion_stats`. Replace
`from pep_oracle.query import format_timestamp` with the extracted helper.

**`format_timestamp` extraction:** move it from `query.py` into `mcp_server.py`
(its only product consumer) or a tiny `formatting.py` util, then delete `query.py`.

### Config / flag cleanup

- Remove `PEP_ORACLE_SERVE_FROM_ARTIFACT` and the now-dead ChromaDB serving branch
  — artifact serving is the only path. Update `get_serving_corpus` and any tests.
- Keep the `pep-oracle-server` console script (local MCP dev), now product-only.
- `.env.example`: remove dead keys; keep what the Lambda/Fargate contract uses.

### Dependencies (`pyproject.toml`)

- Remove `fastembed`, `chromadb`, `pydub` from `dependencies`.
- `embeddings.py`: remove the fastembed backend; Bedrock is the only backend.
  Drop `PEP_ORACLE_EMBED_BACKEND` branching (or hard-fail if set to `fastembed`).
- Confirm nothing else imports the removed packages (e.g. `audioop-lts` was a
  `pydub`/py3.13 shim — remove if now unused).
- Update `Dockerfile` and `Dockerfile.ingest` comments that reference
  chromadb/fastembed as unused base deps; images shrink.

## Tests

- **Delete:** `test_web_conversation.py`, `test_web_episodes.py`,
  `test_web_live.py`, `test_web_responsive.py`, `test_topics.py`,
  `test_topics_io.py`, `test_topics_endpoint.py`, `test_ingest_topics.py`,
  `test_clean_episode_topics.py`, `test_query.py`, `test_backup.py`,
  `test_backfill.py`, `test_references.py`, `test_remap_speakers.py`.
- **Rewrite:** `test_cli.py` (→ `ingest-artifact` + `eval-retrieval` only),
  `test_server.py` (drop GUI-route tests; keep health/version/freshness/MCP/OAuth),
  `test_store.py` (→ the surviving split: `get_ingestion_stats`,
  `_chunk_metadata`, `SENTINEL_NO_TIME`), `test_ingest.py` (→
  `episode_chunks_and_embeddings` only), `test_eval_retrieval.py` (→ corpus-only),
  `test_embeddings.py` (→ Bedrock-only).
- **Keep:** `test_data_integrity_live.py` (validates diarized `has_speaker_*`
  metadata — still relevant to the artifact), `test_mcp_server.py`, `test_oauth*`,
  `test_signing.py`, `test_hybrid.py`, `test_lexical.py`, `test_temporal.py`,
  `test_corpus.py`, `test_ingest_artifact.py`, `test_version.py`, `test_smoke*`,
  `test_feed.py`, `test_chunking.py`, `test_diarize.py`, `test_manager.py`,
  `test_storage.py`, `test_cache.py`.

## Docs / deploy

- **`CLAUDE.md`:** substantial rewrite — remove the CLI-usage block, the web-UI
  sections, and the architecture prose about the Query/`/ask` pipeline, topic
  chips, local ChromaDB ingest, `store.py` ChromaDB, backup, and the embedding
  backend's fastembed default. Keep MCP, corpus artifact, hybrid/temporal,
  ingestion (Fargate), OAuth, and AWS deployment. This runs through the
  `/claude-md-improver` gate as part of the implementation commit. Keep it under
  the ~300-line ceiling (the removal should net-shrink it).
- **`deploy/`:** remove references to `pep-oracle-backup.service` (orphaned by
  `backup.py` deletion). Full `deploy/` teardown (the OptiPlex systemd units,
  cloudflared) stays with the **OptiPlex decommission** task — out of scope here.

## Verification

- `uv run pytest` green (root), `cd infra && python -m pytest` green.
- `cd infra && npx cdk synth '*' -c allowed_email=ci@example.com` green (no infra
  changes expected, but synth must stay green).
- **Both docker images build:** `docker build -f Dockerfile .` and
  `docker build -f Dockerfile.ingest .`.
- **Smoke against the deployed product** (`scripts/smoke.py` / `test_smoke_live`):
  `/health`, `/version`, `/.well-known/...`, `/mcp` (no-token 401 + minted-JWT
  `initialize`/`tools/list`/`tools/call`) all pass — none touch CLI/GUI, so a
  correct excision leaves them green.
- Import-sanity: `python -c "import pep_oracle.server, pep_oracle.mcp_server,
  pep_oracle.ingest_artifact"` succeeds with `fastembed`/`chromadb` uninstalled.

## Out of scope / sequencing

- **Portable-dev-env spec** — reworked against this slimmed tree afterward (no
  Playwright in the devcontainer, trimmed secrets, clean-room verify on the new
  test set).
- **OptiPlex decommission** — stop/disable the systemd units + cloudflared, drop
  local `~/.pep-oracle` ChromaDB, remove `deploy/` units. Separate gated task; the
  code-side overlap (removing local-ChromaDB modules) lands here.
- **Phase 5 KMS signing**, corpus gap backfill — unrelated.

## Risks / things to confirm during implementation

- **`get_ingestion_stats` on `InMemoryCorpus`.** It currently types its arg as a
  `chromadb.Collection` but is documented as a drop-in for the artifact corpus
  (`corpus.py:111`). Confirm it only uses the generic surface (`.name`, `.count()`,
  `.get(...)`) and retype it; this is the function that feeds the MCP `corpus`
  field and must keep working on the artifact.
- **`_build_where` / other private helpers** may have non-obvious callers (e.g.
  `hybrid` filtering). Grep each private function in `store.py` before deleting.
- **`eval-retrieval` live path.** `evaluate_corpus` is already artifact-oriented;
  confirm the legacy live-ChromaDB `evaluate()` is the only thing pulling
  ChromaDB, and that `test_eval_retrieval_live` (corpus-based) still passes.
- **`audioop-lts` / transcripts.** Confirm removing `pydub` doesn't break
  `transcripts/` (the Modal images apt-install ffmpeg; host code shouldn't need
  pydub). Remove `audioop-lts` only if nothing imports `audioop`.
- **`embeddings.py` Bedrock-only.** Local retrieval/MCP dev now needs Bedrock
  creds; ensure tests mock `embed_texts` so CI needs no AWS.
