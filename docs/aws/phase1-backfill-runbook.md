# Phase 1 backfill runbook — Bedrock re-embed of the existing corpus

One-time migration: re-embed the 95 ingested episodes with Bedrock Titan v2 and
publish `v0001` of the corpus artifact. No Modal/GPU; transcription/diarization
are not re-run. Cost: one Bedrock pass over ~10k short texts (a few cents).

## Prerequisites
- AWS credentials with `bedrock:InvokeModel` on `amazon.titan-embed-text-v2:0` in
  `ap-southeast-2`, and (if publishing to S3) `s3:PutObject` on the target bucket.
  Bedrock model access for Titan Text Embeddings V2 must be enabled in the account
  (Bedrock console → Model access).
- `uv pip install -e ".[aws]"`

## Steps

1. Export the current corpus from the box that holds the ingested ChromaDB:
   ```bash
   uv run pep-oracle export /tmp/corpus-export.json
   ```

2. Re-embed + publish (local artifact first, to inspect before S3). `--out` is the
   BASE; the artifact lands at `<base>/corpus/v0001.parquet`:
   ```bash
   export PEP_ORACLE_EMBED_BACKEND=bedrock
   export PEP_ORACLE_BEDROCK_REGION=ap-southeast-2
   export AWS_PROFILE=...   # or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
   uv run pep-oracle backfill --export /tmp/corpus-export.json --out ~/.pep-oracle --version v0001
   ```
   Reports chunk count, episode range, model, and sha256. Artifact lands at
   `~/.pep-oracle/corpus/v0001.parquet`.

3. Validate retrieval quality (no-regression gate). Compare the two reports —
   the Titan hybrid recall@10 / MRR must hold vs the bge-large baseline:
   ```bash
   # bge-large baseline (default backend), over the live ChromaDB:
   PEP_ORACLE_EMBED_BACKEND=fastembed uv run pep-oracle eval-retrieval
   # Titan artifact (query embedder must also be Titan):
   PEP_ORACLE_EMBED_BACKEND=bedrock   uv run pep-oracle eval-retrieval --corpus ~/.pep-oracle
   ```
   Gate: artifact `recall@10` ≥ baseline `recall@10` (and MRR within ~0.02). If
   Titan regresses, re-run the backfill with Cohere (`PEP_ORACLE_EMBED_MODEL=cohere.embed-english-v3`,
   `PEP_ORACLE_EMBED_DIMS=1024`) and re-compare; pin the winner.

4. Publish to S3 (once a bucket exists — Phase 2/3 CDK creates the prod bucket;
   for now any private bucket in `ap-southeast-2` works for validation). Artifact
   lands at `s3://<bucket>/corpus/v0001.parquet`:
   ```bash
   uv run pep-oracle backfill --export /tmp/corpus-export.json --out s3://<bucket> --version v0001
   ```

5. Inspect the artifact (optional):
   ```bash
   uv run python -c "import pyarrow.parquet as pq; t=pq.read_table('$HOME/.pep-oracle/corpus/v0001.parquet'); print(t.schema); print(t.num_rows)"
   ```
