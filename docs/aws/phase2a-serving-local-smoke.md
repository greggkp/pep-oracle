# Phase 2a — serving the MCP tool from the corpus artifact (local smoke)

Phase 2a lets the MCP tool retrieve from the Phase 1 corpus artifact instead of
ChromaDB, gated by `PEP_ORACLE_SERVE_FROM_ARTIFACT`. The OptiPlex keeps its default
(ChromaDB) because nothing rebuilds the artifact on ingest until Phase 3.

## Serve from the artifact locally

Requires a built artifact (see `docs/aws/phase1-backfill-runbook.md`) and Bedrock
creds (the query embedder must be Titan, matching the artifact):

```bash
export PEP_ORACLE_SERVE_FROM_ARTIFACT=1
export PEP_ORACLE_CORPUS_URI=~/.pep-oracle          # base; /corpus is appended
export PEP_ORACLE_EMBED_BACKEND=bedrock
export PEP_ORACLE_BEDROCK_REGION=ap-southeast-2
uv run pep-oracle-server      # or: uvicorn pep_oracle.server:app
```

Then:
```bash
curl -s localhost:8000/version | python -m json.tool   # corpus_version, embed_model, episode_range
```

If the active embedder doesn't match the artifact's `embed_model`, the first MCP
query raises a clear "query embedder mismatch" error (by design — bge-large queries
against a Titan corpus would be meaningless). `GET /version` surfaces the same via
`corpus_error` if the artifact can't be loaded.

## The Lambda entrypoint
`pep_oracle.server:handler` is the Mangum adapter (the same `app`); Phase 2c points
the Lambda at it. Locally it's unused — `uvicorn`/`pep-oracle-server` run the app directly.
