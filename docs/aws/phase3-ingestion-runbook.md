# Phase 3 — AWS ingestion deploy + decommission runbook

Execute after merge, with go-ahead. Region ap-southeast-2, account 940831808393, profile optiplex-cli.
CDK CLI is local: `cd infra && PATH="$PWD/.venv/bin:$PATH" ./node_modules/.bin/cdk ...`.

## 1. One-time prerequisites
- **Modal tokens → SSM SecureString:**
  ```bash
  aws ssm put-parameter --name /pep-oracle/modal-token-id --type SecureString \
    --value "$MODAL_TOKEN_ID" --region ap-southeast-2
  aws ssm put-parameter --name /pep-oracle/modal-token-secret --type SecureString \
    --value "$MODAL_TOKEN_SECRET" --region ap-southeast-2
  ```
- **Speaker references → S3** (one-time; static):
  ```bash
  aws s3 cp ~/.pep-oracle/speaker_profiles.json \
    s3://pep-oracle-corpus-prod/refs/speaker_profiles.json --region ap-southeast-2
  ```
- Confirm the Modal apps are deployed (`modal deploy cloud/transcribe_modal.py cloud/diarize_modal.py`) — unchanged from today.

## 2. Deploy
```bash
cd infra
PATH="$PWD/.venv/bin:$PATH" ./node_modules/.bin/cdk deploy PepOracleIngestStack \
  --require-approval never -c allowed_email=<you@example.com> -c git_sha=$(git -C .. rev-parse --short HEAD)
```
(Builds + pushes the ingest image; creates the VPC, ECS cluster, Fargate task def, daily EventBridge rule, IAM.)

## 3. Verify with a manual run (before relying on the schedule)
**Do not trigger a manual run if the daily schedule could fire concurrently** (two overlapping runs can leave `current.json` pointing at the other run's parquet → a transient sha-mismatch that self-heals on the next run). Disable the rule first if unsure: `aws events disable-rule --name <DailyIngest> --region ap-southeast-2`, re-enable after.

Get the cluster + task-def names from the stack:
```bash
aws cloudformation describe-stack-resources --stack-name PepOracleIngestStack --region ap-southeast-2 \
  --query "StackResources[?ResourceType=='AWS::ECS::Cluster' || ResourceType=='AWS::ECS::TaskDefinition'].[ResourceType,PhysicalResourceId]" --output text
# also grab a public subnet id + the task's security group from the VPC:
aws ec2 describe-subnets --filters Name=tag:Name,Values='*IngestVpc*' --region ap-southeast-2 \
  --query "Subnets[?MapPublicIpOnLaunch].SubnetId" --output text
```
Run one task (substitute the names/ids):
```bash
aws ecs run-task --cluster <cluster-name> --task-definition <task-def-arn> \
  --launch-type FARGATE \
  --network-configuration '{"awsvpcConfiguration":{"subnets":["<public-subnet>"],"assignPublicIp":"ENABLED"}}' \
  --region ap-southeast-2
# find the log group (CDK auto-names it), then tail:
aws logs describe-log-groups --query "logGroups[?contains(logGroupName,'Ingest')].logGroupName" --output text --region ap-southeast-2
aws logs tail <that-log-group> --follow --region ap-southeast-2
```
Confirm a new corpus version published + the live endpoint advanced (serving TTL ~5 min):
```bash
curl -s https://pep-oracle.iicapn.com/version | jq '{corpus_version, corpus_episode_range}'
```
Expect `corpus_version` to advance (e.g. v0002) and `corpus_episode_range` max to include the newest episode.

**Selection is newest-forward** (default): the run only ingests numbered episodes *newer* than the corpus's current max episode number. Old back-catalogue gaps and unnumbered "EXTRA" bonus episodes are skipped on purpose — otherwise a single permanent gap would make every daily run a fragile, hours-long all-or-nothing job (publish is one atomic flip after the whole loop, so any mid-run failure → nothing published). So a verify run normally processes just the one newest episode (quick + cheap).

Each publish also writes a prebuilt BM25 index sidecar (`corpus/vNNNN.bm25.zst`, ~6 MB at the current corpus size) next to the parquet — a serving cold-start optimization; a failure to write it only logs a warning and never blocks the publish. See [prebuilt-bm25-index.md](prebuilt-bm25-index.md).

## 4. Cut over (only after step 3 succeeds)
On the OptiPlex:
```bash
sudo systemctl disable --now pep-oracle-ingest.timer
sudo systemctl disable --now pep-oracle-backup.service
```
AWS is now the sole ingest + publish path (no parallel run).

**Backup after cutover:** the corpus lives in the **versioned** S3 bucket (durable, rollback-able); speaker refs live in S3. The off-site copy of the Modal transcript/diarization caches stops — accepted (re-compute insurance only; forward ingestion re-transcribes only new episodes, and processed chunks are already in the artifact). The OptiPlex `pep-oracle-api.service` stays as the DNS-rollback fallback (note: its corpus goes stale once local ingest stops) until a later explicit decommission.

## Rollback
- Stop AWS ingestion without data loss: `aws events disable-rule --name <DailyIngest rule> --region ap-southeast-2` (the job is idempotent — no half-published state).
- Resume the OptiPlex: `sudo systemctl enable --now pep-oracle-ingest.timer` (and `pep-oracle-backup.service` if wanted).

## Backfilling the back-catalogue gap (supervised, optional)
The serving corpus has a real gap: **episodes 179–216 were never transcribed anywhere** (no Modal cache), plus the unnumbered "EXTRA" bonus episodes aren't in the artifact. Newest-forward ingestion (the daily default) will never pick these up — they're older than the corpus max. To fill them while they're still in the feed's rolling ~100-entry window, run a deliberate backfill:
```bash
# one-shot ECS task with --backfill (or run locally with EMBED_BACKEND=bedrock + CORPUS_URI=s3://pep-oracle-corpus-prod)
aws ecs run-task ... --overrides '{"containerOverrides":[{"name":"ingest","command":["ingest-artifact","--backfill"]}]}'
```
This ingests EVERY feed episode the corpus lacks (49 as of 2026-06-08): ~3 hrs of fresh Modal GPU, all-or-nothing (one bad episode → nothing published, redo next run). **Disable the daily rule first** so it can't overlap, run supervised, then re-enable. Episodes that roll out of the 100-entry feed window before a successful backfill are unrecoverable via this path (they'd need a manual re-ingest with audio still available).
