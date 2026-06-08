# Phase 3 — AWS-native ingestion (design)

**Status:** design approved 2026-06-08 (brainstormed). Next: implementation plan.
**Part of:** the AWS MCP migration (`docs/superpowers/specs/2026-06-02-aws-mcp-migration-design.md`). Phase 2 (serving) is live on AWS; this is Phase 3 (ingestion).

## Goal

Make new podcast episodes flow to the live AWS endpoint automatically, with **no OptiPlex in the loop**. Today the OptiPlex is the only ingestion path (it writes ChromaDB locally), and the AWS serving corpus is a static `v0001` artifact — new episodes never reach `pep-oracle.iicapn.com` without a manual rebuild+upload. Phase 3 moves ingestion to a scheduled, scale-to-zero AWS job that publishes new corpus versions to S3, which the serving Lambda already picks up via its TTL refresh.

## Architecture

`EventBridge` schedule (daily) → `ECS Fargate` task (scale-to-zero) running an **artifact-native incremental ingest** → publishes a new corpus version (`vN+1.parquet` + manifest, atomic `current.json` flip) to the existing `pep-oracle-corpus-prod` S3 bucket. The serving Lambda's warm containers re-read `current.json` on their ~5-min TTL, so new episodes appear within minutes of a successful run — no serving redeploy, no coupling of code and corpus versions.

No ChromaDB anywhere on this path: the corpus artifact (parquet, loaded in memory) is the source of truth for "what's already ingested."

## Ingest flow (`ingest_artifact` — the new orchestrator)

1. **Load the current corpus** via `corpus.load_current(CORPUS_URI)` → in-memory rows (ids, docs, embeddings, metadatas). Collect the set of already-ingested episode GUIDs from the row metadata (`episode_guid`).
2. **Find new episodes:** `feed.fetch_episodes()` → new = feed episodes whose GUID isn't in the loaded set. (No `--force`/re-ingest in the routine path.)
3. **Process each new episode** (reusing the existing per-episode pipeline): Modal transcribe (`cloud/transcribe_modal`) + diarize (`cloud/diarize_modal`) → `apply_diarization` + speaker labelling via `assign_by_voice` using the **`speaker_profiles.json` references read from S3** → `chunking` → **Bedrock embed the new chunks only** → produce chunk records `{chunk_id, text, embedding, metadata}` matching the parquet schema.
4. **Merge + publish:** append the new chunk records to the loaded corpus rows → `corpus.write_artifact(vN+1)` (writes parquet + manifest with the merged `episode_range`/`chunk_count`) → verify sha256 → **atomically flip `current.json`**.
5. **No new episodes → no-op:** exit 0 without publishing (the Lambda keeps serving the current version).

**Idempotent by construction:** a failed run publishes nothing (the flip is last + atomic), so the next scheduled run re-detects the same new episodes and retries. Partial Modal/embed work is simply redone — only chunk records that make it into a published version count as "ingested."

## Code: reuse vs new

**Reused unchanged:** `feed`, `cloud/transcribe_modal`, `cloud/diarize_modal`, the diarization speaker-assignment (`diarize.apply_diarization`/`assign_by_voice`/`assign_substantive_speakers`), `chunking`, the Bedrock embedding backend (`embeddings` with `EMBED_BACKEND=bedrock`), and `corpus.load_current`/`write_artifact`/`Manifest`.

**Refactor (small):** extract the per-episode "transcribe → diarize → chunk → embed → produce chunk records" logic from `ingest.py` into a reusable function that returns chunk records, so **both** the existing ChromaDB path (unchanged behavior for local/OptiPlex use, kept until decommission) and the new artifact path call the same per-episode code. The only differences in the new path are the *source of truth* for incremental detection (artifact GUIDs, not ChromaDB) and the *sink* (artifact rows, not ChromaDB upsert).

**New:** the `ingest_artifact` orchestrator (steps 1–5 above) + a CLI entrypoint (e.g. `pep-oracle ingest-artifact`) that the Fargate container runs. It requires `EMBED_BACKEND=bedrock` and `CORPUS_URI` pointing at the prod bucket; it reads `speaker_profiles.json` from a configured S3 location.

## Infra (added to `infra/`, CDK Python)

Added to `PepOracleProdStack` (or a sibling stack in the same app):
- **ECS cluster** (no EC2 — Fargate only) + a **Fargate task definition**.
- **Ingest container image** (new `Dockerfile.ingest` or a build target): the package + `modal` + `boto3` + the ingest deps (base + `aws` extra; **no** FastAPI/MCP/fastembed), entrypoint = the `ingest-artifact` CLI. Built as a CDK `DockerImageAsset`.
- **EventBridge rule** (daily `schedule`) → `EcsTask` target → `RunTask` on the task def (`FARGATE`, `awsvpc`, public subnet or NAT for egress to Modal/Bedrock/S3/RSS).
- **Least-privilege task role:** `bedrock:InvokeModel` on the embed-model ARN; `s3:GetObject`+`s3:PutObject` on the corpus prefix; `s3:GetObject` on `speaker_profiles.json`; `ssm:GetParameter` on the Modal-token params; `kms:Decrypt`+`kms:GenerateDataKey` on the data key (the corpus bucket is CMK-encrypted, so writes need it); CloudWatch Logs.
- **Modal tokens → SSM SecureString** (`MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`), injected into the task via the ECS task-definition `secrets` mechanism (never baked into the image).
- Reuse the existing data KMS key + corpus bucket from Phase 2c.

**Networking note:** Phase 2c is VPC-less (Lambda needs no VPC), but Fargate (`awsvpc`) requires a VPC + subnets — Phase 3 uses the account **default VPC** or a minimal new one (decided in the plan). Fargate needs outbound internet (Modal API, the RSS enclosure host, Bedrock, S3, SSM): default to a public subnet with a public IP (simplest, scale-to-zero, no NAT cost); S3/Bedrock/SSM can use VPC endpoints later if egress lockdown is wanted (out of scope).

## Concurrency & cadence

- **Cadence:** EventBridge **daily**. Scale-to-zero — the task only does Modal/Bedrock work when there are new episodes; a no-new-episodes run is a cheap feed check.
- **Concurrency:** the daily interval far exceeds a run's duration, and the atomic `current.json` flip plus "only-new-from-the-published-artifact" detection make overlap safe. A lightweight guard prevents a pathological double-publish (e.g., the EventBridge target's built-in single-run behavior, or a short DynamoDB/S3 conditional lock). Detailed mechanism chosen in the plan.

## State / inputs

- **Corpus artifact:** loaded from + published to `s3://pep-oracle-corpus-prod/corpus/` (the Phase 2c bucket). The artifact is the incremental source of truth + the durable, versioned store.
- **Speaker references:** `speaker_profiles.json` uploaded **once** to S3 as a static diarization input (the two hosts' voice references are stable). Regenerating references (`build-references`, GPU path) is rare and out of scope; if ever needed it's a deliberate manual step.

## Transition / decommission (verify-then-cut)

1. Deploy Phase 3.
2. Trigger one Fargate run manually; verify it publishes a new corpus version and the live endpoint reflects it.
3. **Then** disable the OptiPlex `pep-oracle-ingest.timer` and **turn off `pep-oracle-backup.service`** — AWS is the sole ingest + publish path, no parallel run.

**Backup after cutover:** the corpus lives in the **versioned** S3 bucket (the durable, rollback-able copy) and `speaker_profiles.json` lives in S3. The off-site copy of the **Modal transcript/diarization caches stops** — accepted: forward ingestion re-transcribes only new episodes, and the processed chunks are already in the corpus artifact, so the caches are re-compute insurance for a rare full back-catalogue re-ingest, not data. (No one-time cache copy to S3 — decided to let them go.)

The OptiPlex `pep-oracle-api.service` stays as the DNS-rollback fallback (revert the NS delegation → it serves) until a later, explicit decommission; note its corpus goes stale once local ingest stops.

## Testing

- **Unit (repo pytest, mocks like the existing suite):** the incremental merge — artifact load → feed diff → process-only-new (Modal/Bedrock mocked) → merged `vN+1` rows with correct GUIDs/episode_range/chunk_count; the no-new-episodes no-op; idempotency (a failed publish leaves `current.json` unchanged).
- **CDK assertion tests (`infra/tests`):** the Fargate task def (image, role), the EventBridge daily rule → RunTask, IAM least-privilege (bedrock/s3/ssm/kms), Modal tokens via SSM secrets.
- **Deploy check:** a live one-shot Fargate run that ingests any pending episodes and publishes, verified via `GET /version` (corpus version + episode_range advance).

## Out of scope

Topics + the web UI's `/topics`/`/episodes`/`/status` and `/ask` (corpus-only scope; `/ask` needs `ANTHROPIC_API_KEY`, excluded); regenerating speaker references; the one-time Modal-cache copy; CI/CD (Phase 4); KMS asymmetric JWT signing (Phase 5); the deferred Lambda cold-start init-timeout perf item. Migration cleanup owed (orphaned KMS key + Cognito pool from the failed first 2c deploy) is tracked separately.
