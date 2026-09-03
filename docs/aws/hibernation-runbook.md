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

**Ordering is not advisory.** Prod must be deployed before cert, and every deploy
must pass `--exclusively`. `app.py` declares `prod.add_dependency(cert_stack)`, so
without it CDK deploys the cert stack *first* — exactly backwards. When the cert
stack then tries to delete the WebACL while the prod stack's CloudFront
distribution still references it, CloudFormation does not fail the deploy. It
retries through the cleanup phase, gives up, and **orphans the WebACL**: removed
from the stack, still present in AWS, still billing, and no longer visible to any
`cdk deploy`. Recovering means deleting it by hand:

```bash
aws wafv2 delete-web-acl --scope CLOUDFRONT --region us-east-1 \
  --name <name> --id <id> \
  --lock-token $(aws wafv2 list-web-acls --scope CLOUDFRONT --region us-east-1 \
    --query 'WebACLs[0].LockToken' --output text)
```

That is not hypothetical — it is what happened on 2026-08-29 (see *Status*).

**Cancelling a deploy does not cancel it.** Cancelling the GitHub Actions run kills
the CDK CLI, but CloudFormation carries on server-side with whatever changeset was
already submitted. After an aborted deploy, check stack status in **both** regions
(`ap-southeast-2` *and* `us-east-1`) before concluding nothing happened — the cert
stack is the one CDK touches first, and it is the one in the other region.

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
on) and run a supervised catch-up. **Redeploy the Modal apps first** (see *Off-AWS:
Modal*) — ingestion calls them, and while hibernated they are stopped, so a
catch-up run fails without them:

```bash
uv run pep-oracle ingest-artifact           # newest-forward
```

## Off-AWS: Modal

Ingestion spends real money on Modal — A100 GPU time for Whisper transcription and
pyannote diarization — billed per second of execution, separately from AWS.

**Check before assuming.** `modal billing report` is authoritative and takes one
command. Daily reports cannot span more than 31 days, so use two windows for a
longer view:

```bash
modal billing report --start "30 days ago" --json
```

**At this scale Modal bills compute only.** Across the 60 days to 2026-09-03 the
report returned ten line items, every one of them per-run GPU against
`pep-oracle-transcribe` or `pep-oracle-diarize`, and **no storage line at all**:
$1.06 + $1.18 = $2.23, roughly $1.10/month, charged only on the five days an
episode was actually ingested. Do not assume volumes are costing anything — measure.

**The lever is the ingest schedule, and nothing else.** The daily Fargate task is
the only automated caller, so disabling its EventBridge rule takes Modal spend to
zero on its own; a deployed-but-idle app runs nothing. Stopping the apps adds one
thing the schedule cannot: it closes the path of someone running `pep-oracle
ingest-artifact` by hand.

```bash
modal app stop -y pep-oracle-transcribe
modal app stop -y pep-oracle-diarize
```

**Deleting the model-cache volumes is optional and, on the evidence above, saves
nothing.** It is recorded here because it was done on 2026-09-03 and because the
reasoning is worth having if storage ever does start billing: both volumes are
declared `modal.Volume.from_name(..., create_if_missing=True)`
(`cloud/transcribe_modal.py`, `cloud/diarize_modal.py`), so deleting them is
self-healing — the next run recreates the volume and re-downloads the weights with
no code change and no manual restore step. The only cost is a slower first episode.

```bash
modal volume delete -y pep-oracle-whisper-cache
modal volume delete -y pep-oracle-pyannote-cache
```

**Keep the `huggingface-token` Modal secret.** It is free, pyannote needs it to
re-download weights, and recreating it means going back to Hugging Face.

### Restoring Modal

```bash
modal deploy cloud/transcribe_modal.py
modal deploy cloud/diarize_modal.py
```

The volumes recreate themselves on the first run — nothing to restore by hand.
Expect the first episode after a restore to be slow while `large-v3-turbo` and the
pyannote weights download again.

One incidental effect of having deleted the whisper cache: it had accumulated
`models--Systran--faster-whisper-large-v3` alongside the
`models--mobiuslabsgmbh--faster-whisper-large-v3-turbo` that the code actually
loads, so it was storing a model no longer in use. The rebuilt cache holds only the
turbo model.

## Status

**Shut down.** AWS hibernated 2026-08-29; Modal stopped 2026-09-03. Verified at
each step.

AWS — prod and ingest deployed hibernated via the `deploy` workflow (run
33276339470, v1.4.9 input):

- serving Lambda gone; HTTP APIs 0; CloudFront distributions 0
- Route 53 A-alias removed — `pep-oracle.iicapn.com` resolves to nothing
- both ingest schedules `DISABLED`; the 4-minute warmer rule gone
- WebACL deleted; ECR emptied (68 images)
- corpus `v0010.parquet` + manifest intact in S3; OAuth table `ACTIVE`

Modal — both apps `stopped` with 0 tasks; both volumes deleted; the
`huggingface-token` secret kept. Note the volume deletion saved nothing
measurable: the billing report shows compute-only charges, and Modal spend had
already gone to zero on 2026-08-27 when the ingest schedule was disabled.

The workflow run finished **red at the smoke test**, which is correct — no endpoint
remains to smoke — so no `v1.4.9` tag was pushed. The last released tag is
`v1.4.8`, and `v1.4.9` is still an unused version number.

Residual AWS spend is **~$1.95/month**: KMS $1.00, Route 53 $0.50, Secrets Manager
$0.40, S3 $0.03.

### How the WebACL actually went

Not through this switch. An earlier deploy attempt was cancelled mid-flight;
CloudFormation carried on server-side, deployed the cert stack first (see *Ordering
is not advisory*), failed four times to delete the WebACL while the distribution
still referenced it, and **orphaned** it — removed from the stack, still live in
AWS, still billing, invisible to `cdk deploy`. Clearing it needed a hand-run
`aws wafv2 delete-web-acl` as root via CloudShell, after the distribution was gone.
`--exclusively` in `deploy.yml` now prevents the inversion that caused it.

The cert stack is otherwise in its hibernated shape (cert, zone, WAF log group,
alerts topic), so hibernation needs no further cert deploy. A restore deploys the
cert stack first and creates a *new* WebACL.

`release-train.yml` is disabled at the repo level. **Re-enable it on restore**:
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

Modal is billed separately and was **~$1.10/month** — $2.23 over the 60 days to
2026-09-03, entirely per-run GPU time on the five days an episode was ingested,
with no storage component (`modal billing report`). It goes to zero with the
ingest schedule.

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
