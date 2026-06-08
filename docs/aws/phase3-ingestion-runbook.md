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
aws logs tail /aws/ecs/... --follow --region ap-southeast-2     # watch it run
```
Confirm a new corpus version published + the live endpoint advanced (serving TTL ~5 min):
```bash
curl -s https://pep-oracle.iicapn.com/version | jq '{corpus_version, corpus_episode_range}'
```
Expect `corpus_version` to advance (e.g. v0002) and `corpus_episode_range` to include the newest episode.

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
