# pep-oracle

An **MCP server** that answers US-politics questions over the *PEP with Chas and Dr Dave* podcast using retrieval-augmented generation (RAG). Episodes are transcribed, diarized, chunked, embedded, and published as a versioned corpus artifact; a frontier model calls a single MCP tool to retrieve grounded, citable excerpts.

Runs entirely on AWS: a serving Lambda (the MCP tool) plus a scheduled Fargate ingestion job. There is no user-facing CLI query path or web GUI — the product *is* the MCP tool.

## How it works

```
RSS feed ──▶ transcribe (Whisper on Modal) ──┐
         └─▶ diarize (pyannote on Modal) ─────┴─▶ align speakers ──▶ chunk
                                                                        │
                                                          embed (Bedrock Titan)
                                                                        │
                                              publish versioned corpus artifact (S3 parquet)
                                                                        │
   MCP client ──▶ search_us_politics_commentary ──▶ hybrid retrieval ──┘
                       (serving Lambda)            + temporal reranking ──▶ citations
```

There are two pipelines:

- **Ingestion** (`ingest_artifact.py`, run on Fargate): finds new feed episodes, transcribes them with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on a Modal A100 GPU, diarizes with pyannote (also on Modal), aligns speaker turns and maps host names, chunks on time windows with overlap, embeds with AWS Bedrock Titan, and publishes a new immutable corpus version (atomic `current.json` flip).
- **Retrieval** (the MCP tool, served from Lambda): embeds the query, runs **hybrid search** (semantic embeddings + BM25 lexical, merged with weighted Reciprocal Rank Fusion), applies **intent-gated temporal reranking**, and returns citation dicts (episode, title, date, timestamp, speakers, excerpt).

### Key design choices

- **Hybrid retrieval** (`hybrid.py` + `lexical.py`) — semantic search blurs the distinctive terms (proper nouns, bill names) a politics podcast is full of; BM25 nails those but is blind to paraphrase. The two are fused so each rescues the other's weakness.
- **Intent-gated recency** (`temporal.py`) — a blanket recency prior makes old-but-relevant content unretrievable. Recency is applied only when the query intent (`current` / `historical` / `evolution` / `prediction` / `timeless`) calls for it.
- **Versioned corpus artifact** (`corpus.py`) — an immutable parquet (`vNNNN.parquet` + manifest) is the single serving source, TTL-refreshed with atomic version swaps. No vector DB.
- **Bedrock-only embeddings** (`embeddings.py`) — `amazon.titan-embed-text-v2:0`, 1024-dim. Query and corpus vectors are validated to come from the same model.
- **OAuth-gated MCP** (`mcp_server.py` + `oauth.py`) — the single tool is exposed over the MCP Streamable HTTP transport behind an in-app OAuth 2.1 + DCR provider (JWT bearer verification).

See [`CLAUDE.md`](CLAUDE.md) for the full architecture reference and rationale.

## Quickstart

```bash
# uv manages the venv — no activate needed
uv pip install -e ".[server]"

# run the unit tests (external APIs are mocked)
uv run pytest

# local MCP server (dev; prod runs the same app as a Lambda)
uv run pep-oracle-server
```

For the full dev setup (devcontainer, bootstrap script, secrets, AWS access), see **[`SETUP.md`](SETUP.md)**.

## Common commands

```bash
# Incremental ingest: publish a new corpus version with new feed episodes
uv run pep-oracle ingest-artifact                 # newest-forward (default)
uv run pep-oracle ingest-artifact --backfill      # supervised catch-up of old gaps

# Retrieval-quality score (recall@k, MRR) over a corpus artifact
uv run pep-oracle eval-retrieval --corpus <s3://… | local-path>

# Tests
uv run pytest                                     # unit (live tests excluded by default)
uv run pytest -m live                             # include live tests (real APIs/corpus)
```

## Deployment

The product runs entirely on AWS. `infra/` is a CDK (Python) app with three concerns:

- **Serving** — container Lambda (`pep_oracle.server.handler`) fronted by CloudFront → API Gateway HTTP API, with S3 corpus storage, a DynamoDB OAuth table, KMS, and a one-user Cognito pool.
- **Ingestion** — a daily EventBridge rule triggering a scale-to-zero Fargate task running `pep-oracle ingest-artifact`.
- **CI/CD** — `ci.yml` gates every PR (ruff + pytest + CDK synth + Docker builds); `deploy.yml` deploys on a `v*` tag (or `workflow_dispatch`) via GitHub OIDC, then smoke-tests.

Dependency security patches are automated: Dependabot opens grouped security PRs → CI gates them → patch/minor updates auto-merge → a release is cut and the production deploy pauses for one-click approval.

Runbooks live under [`docs/aws/`](docs/aws/).

## Repository layout

| Path | What |
|---|---|
| `src/pep_oracle/` | Application code (ingestion, retrieval, MCP server, OAuth, corpus) |
| `cloud/` | Modal apps for GPU transcription + diarization |
| `infra/` | CDK app (serving, ingestion, CI/CD stacks) |
| `docs/aws/` | Deployment runbooks and design notes |
| `tests/` | Unit tests (mocked APIs) + `@pytest.mark.live` integration tests |
| `scripts/` | Bootstrap, smoke test, benchmarks |
