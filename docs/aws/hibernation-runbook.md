# Hibernation runbook

How to take pep-oracle offline so it costs almost nothing, and how to bring it
back. Current state and the numbers behind it are recorded at the bottom.

## Why there is a switch instead of a teardown

Two facts drive the design.

**The bill is fixed per-resource charges, not usage.** Measured over July and
August 2026, serving traffic cost nothing: Lambda, API Gateway, CloudFront,
DynamoDB, Cognito, EventBridge and CloudWatch all sit inside the perpetual free
tier at this volume, and the 4-minute warmer is free along with them. The money
goes to resources that bill for existing — the WAF WebACL above all, at
$0.258/day whether or not a request ever reaches it. So "stop using it" saves
nothing; hibernation has to actually delete resources.

**`cdk destroy` is a one-way door here.** The data resources are
`RemovalPolicy.RETAIN` *with fixed physical names* — the corpus bucket
(`pep-oracle-corpus-prod`), the OAuth table (`pep-oracle-oauth`), the Cognito
domain prefix (`pep-oracle-prod`) and the client secret
(`pep-oracle/cognito-client-secret`). Destroying the stack orphans all of them,
still billing, and the next `cdk deploy` fails trying to re-create names that
already exist. Recovering from that means `cdk import` on each one.

So hibernation is a deploy-time flag that removes the public surface and the WAF
while leaving every named, retained resource in the template.

## What the flag does

`hibernate` (in `infra/cdk.json`, overridable with `-c hibernate=true|false`):

| Stack | Hibernated | Kept |
|---|---|---|
| `PepOracleCertStack` | WAF WebACL, its logging config, blocked-request alarm | hosted zone, ACM cert, WAF log group (fixed name + RETAIN), alerts topic |
| `PepOracleProdStack` | serving Lambda, HTTP API + access logs, CloudFront, Route 53 A-alias, warmer rule, all six alarms | KMS key, corpus bucket, CDN log bucket, DynamoDB OAuth table, Cognito pool/domain/client, client secret, alerts topic |
| `PepOracleIngestStack` | both daily schedules (`DailyIngest`, `CorpusStaleCheckSchedule`) set to `DISABLED` | VPC, cluster, task definition, DLQ, alarms — all free at rest |

The zone and cert stay deployed deliberately. They cost $0.50/month between them
and keeping them means restore needs no NS re-delegation at the registrar and no
ACM re-validation. The SNS topics stay because they are free and their email
subscriptions would otherwise need re-confirming by hand.

Disabling rather than deleting the ingest schedule also stops the **Modal** GPU
spend (transcription + diarization), which is billed off-AWS and is the larger
per-episode cost.

## Hibernate

**Credentials.** These deploys need admin (or at least the CDK bootstrap roles).
The everyday `gregg-cli` key is `ReadOnlyAccess` via the `personal-cli-readonly`
group and fails at asset publish with `not authorized to perform: s3:PutObject`
on the bootstrap assets bucket — it cannot run any of this. `deploy.yml` can
cover the prod and ingest stacks through the GitHub OIDC role, but it does not
deploy `PepOracleCertStack` (manual by design) and its smoke test fails by
definition while hibernated, so the WAF removal — the whole point — needs admin
credentials locally.

```bash
cd infra
# 1. Flip the flag (committed, so an unattended deploy can't silently undo it).
#    "hibernate": true in cdk.json
# 2. Deploy prod FIRST: it owns the CloudFront distribution, and the WebACL in
#    the cert stack cannot be deleted while a distribution is associated with it.
#    --exclusively matters: without it, `cdk deploy PepOracleProdStack` pulls in
#    PepOracleCertStack as a dependency and deploys it first, which attempts
#    exactly that forbidden WebACL deletion.
node_modules/.bin/cdk deploy PepOracleProdStack --exclusively \
  -c allowed_email=<email> -c alert_email=<email> -c git_sha=$(git rev-parse --short HEAD)
node_modules/.bin/cdk deploy PepOracleIngestStack --exclusively \
  -c allowed_email=<email> -c alert_email=<email> -c git_sha=$(git rev-parse --short HEAD)
node_modules/.bin/cdk deploy PepOracleCertStack --exclusively -c allowed_email=<email>
```

Pass the *currently deployed* `allowed_email` / `alert_email`; changing them
replaces the SNS subscriptions and each new one needs confirming by email.

CloudFront deletion is the slow step (~15 minutes: disable, propagate, delete).
The ingest deploy rebuilds and pushes its container image (the CDK asset hash
covers `infra/`, so any change here invalidates it); on a podman host that is the
step most likely to fail. If it does, the schedules can be disabled directly
instead — the drift is benign and self-correcting, since a later deploy in either
state writes the matching value:

```bash
aws events disable-rule --region ap-southeast-2 --name <PepOracleIngestStack-DailyIngest...>
aws events disable-rule --region ap-southeast-2 --name <PepOracleIngestStack-CorpusStaleCheckSchedule...>
```

Then prune the container images, which are dead weight while hibernated and are
the one line item that grows on its own (~1 image/day while the release train
runs). The bootstrap repo's lifecycle policy only expires *untagged* images, so
tagged asset images accumulate forever:

```bash
aws ecr list-images --repository-name cdk-hnb659fds-container-assets-<acct>-ap-southeast-2 \
  --region ap-southeast-2 --query 'imageIds[*]' --output json > /tmp/imgs.json
# delete in batches of 100
aws ecr batch-delete-image --repository-name cdk-hnb659fds-container-assets-<acct>-ap-southeast-2 \
  --region ap-southeast-2 --image-ids file:///tmp/imgs.json
```

Deleting every image is safe: `cdk-assets` re-builds and re-publishes anything
missing on the next deploy. Do it **after** the prod deploy, not before — until
the Lambda is gone it is still running from one of those images.

Finally, `.github/workflows/release-train.yml`'s cron is commented out. Left
running it would fail its smoke test every morning and push a fresh image daily.
`ci.yml`'s nightly run is deliberately left alone: it costs nothing on AWS and
keeps the dependency/vulnerability tripwire live.

## Restore

```bash
cd infra
# 1. "hibernate": false in cdk.json, and uncomment the release-train cron.
# 2. Cert stack first — the prod CloudFront needs the WebACL ARN to exist. This
#    is the reverse of the hibernate order, for the same association reason.
node_modules/.bin/cdk deploy PepOracleCertStack --exclusively -c allowed_email=<email>
node_modules/.bin/cdk deploy PepOracleProdStack PepOracleIngestStack \
  -c allowed_email=<email> -c alert_email=<email> \
  -c git_sha=$(git rev-parse --short HEAD) -c semver=<vX.Y.Z>
uv run python scripts/smoke.py            # from the repo root
```

> **A restore blocker used to live here; it is fixed.** The deploy of 2026-08-28
> 17:49 UTC failed and rolled back — API Gateway rejected the HTTP API stage's
> access-log format with *"The following context variables are not supported:
> [$context.request.header.mcp-method, $context.request.header.mcp-protocol-version]"*,
> because HTTP APIs resolve no arbitrary request header in an access log. A restore
> would have hit the same error as a *create*, failing the whole stack rather than
> one resource. #66 removed those fields and moved the JSON-RPC method to an app-side
> log line at ASGI entry; it shipped in **v1.4.8** (`04c26ee`), whose deploy cleared
> that step and smoke-tested clean. Nothing to do here — recorded because the
> symptom (a restore failing at the HTTP API stage) would otherwise be baffling.

Or, equivalently, commit the flag flip and run the `deploy` workflow via
`workflow_dispatch` with a `version` input — it does the same deploy plus the
smoke test and tags the commit.

Expect the first deploy to take ~20–30 minutes: it rebuilds and pushes the
serving container image, and CloudFront distribution creation is slow again.

What survives, so what you do *not* have to redo:

- **The corpus.** `pep-oracle-corpus-prod` is untouched, `current.json` included,
  so the restored Lambda serves the same artifact version it had before. No
  re-ingestion, no Modal spend, no re-embedding.
- **DNS and TLS.** Same hosted zone, same NS delegation at Cloudflare, same ACM
  cert. The A-alias is re-created pointing at the new distribution.
- **OAuth state.** The DynamoDB table keeps registered clients and unexpired
  refresh tokens (30-day rotation), so a client that was connected before a short
  hibernation may not even need re-registering. After 30 days, expect to redo the
  MCP connector setup.
- **The Cognito user.** The pool, the hosted-UI domain prefix and the single
  allowed user all persist.

What does change: the CloudFront distribution is new, so its domain differs — the
Route 53 alias is updated by the deploy, but any client pinning the old
distribution domain directly must be re-pointed at `pep-oracle.iicapn.com`.

After restore, check the corpus is behind the feed (the podcast will have moved
on) and run a supervised catch-up:

```bash
uv run pep-oracle ingest-artifact           # newest-forward
```

## Status

Not yet applied. The switch, its tests and this runbook are in place and
`hibernate` is `true` in `infra/cdk.json`, but **no AWS resource has been changed
yet**: the service is up, current (v1.4.8, deployed 2026-08-29 00:51 UTC, stack
`UPDATE_COMPLETE`), and still billing at the rate below.

What is needed to apply it, and by whom:

- **Prod and ingest** can be hibernated by CI. Merging the flag to `main` and
  running the `deploy` workflow assumes the OIDC role and deploys both stacks, no
  local credentials involved. That run will go **red at the smoke test** — there
  is no endpoint once hibernated, which is the point — so it deploys but does not
  tag. Expect the red run rather than reading it as a failure.
- **The cert stack cannot.** `deploy.yml` never deploys it (manual by design), so
  removing the WebACL — 73% of the bill — needs admin credentials locally. The
  everyday `gregg-cli` key is `ReadOnlyAccess` and cannot do it.

`release-train.yml` is already disabled at the repo level (`gh workflow disable`),
independently of the commented-out cron here, so the 06:00 UTC train cannot
re-deploy over a hibernated stack. **Re-enable it on restore**:
`gh workflow enable release-train.yml`.

## Cost record

Measured with Cost Explorer on 2026-08-29 (usage records, credits excluded).
July was a full month; August is 1–28 with the WAF added part-way.

| Service | Jul | Aug (28d) | Note |
|---|---|---|---|
| AWS WAF | — | 3.11 | $0.258/day ≈ $7.85/month |
| KMS | 1.00 | 0.89 | one CMK |
| ECR | 0.58 | 0.78 | 62 images, 15.5 GB |
| Route 53 | 0.50 | 0.50 | hosted zone |
| Secrets Manager | — | 0.16 | Cognito client secret |
| ECS/Fargate | 0.04 | 0.06 | daily ingest runs |
| S3 | 0.02 | 0.02 | 507 MB corpus |
| Bedrock | — | 0.01 | query embeddings |
| **Total** | **2.17** | **5.60** | ~$10.70/month at steady state |

Everything else read $0.00. Hibernated steady state is **~$1.95/month**: KMS
$1.00, Route 53 $0.50, Secrets Manager $0.40, S3 $0.03.

The remaining $1.50 of that is deliberate. Deleting the CMK would brick the
corpus and the OAuth table unless the parquet is copied out to SSE-S3 first, and
key deletion has a 7–30 day pending window; deleting the hosted zone costs an NS
re-delegation and ACM re-validation on the way back. Both are poor trades against
the restore properties above. A true $0 teardown means deleting the corpus, and
rebuilding it means re-transcribing 200+ episodes through Modal — that is the one
genuinely expensive step in this system, and it is what hibernation exists to
avoid.
