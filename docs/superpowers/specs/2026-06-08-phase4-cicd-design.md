# Phase 4 — CI/CD (GitHub OIDC, tag-to-deploy) design

**Status:** design approved 2026-06-08 (brainstormed). Next: implementation plan.
**Part of:** the AWS MCP migration (`docs/superpowers/specs/2026-06-02-aws-mcp-migration-design.md`). Phase 2 (serving) + Phase 3 (ingestion) are live on AWS; this is Phase 4 (CI/CD).

## Goal

Replace manual `cdk deploy` from a laptop (which hit disk-fill twice during the Phase 3 deploy and depends on local creds) with an automated, keyless GitHub Actions pipeline: every PR/push is gated by tests, and a `v*` git tag builds + deploys the app to AWS via short-lived OIDC credentials, then smoke-tests the live endpoint. No long-lived AWS secrets in GitHub.

## Decisions (from brainstorming)

- **Lean, prod-only** — no staging environment. The existing single prod (`PepOracleProdStack` + `PepOracleIngestStack`) is the only target. (Staging + promote-by-digest from the original migration sketch are explicitly deferred — YAGNI for a single-operator service.)
- **`v*` tag triggers deploy** — PR + push-to-main run the test gate only; a `v*` tag deploys. Main stays continuously tested; prod changes only on a deliberate release tag, which becomes the semver in `GET /version`.
- **Keep `from_asset`** — `cdk deploy` builds the images during the deploy job (no rework to consume pre-built ECR digests). Rollback = re-deploy a prior tag.
- **Deploy scope = serving + ingest** — a tag deploys `PepOracleProdStack` + `PepOracleIngestStack`. `PepOracleCertStack` (us-east-1, rarely changes) stays a manual deploy.
- **ruff (lenient)** added as a required CI lint check (one-time cleanup of existing findings).
- **Auth-free smoke** post-deploy (no JWT minting → no SSM grant needed in CI).

## Architecture (reconciled with current reality)

The original migration sketch (Section 3 of the 2026-06-02 design) assumed CloudFront→Function-URL, two environments, and promote-by-digest. The deployed reality differs and this design matches it: front door is **CloudFront → API Gateway HTTP API → Lambda**; only **prod** exists; images are built by CDK `from_asset`. Phase 4 automates *that* system; it does not re-architect it.

### Two workflows (`.github/workflows/`)

**`ci.yml`** — on `pull_request` + `push` to `main`. No AWS access.
```
ruff check  →  pytest (root, 387)  →  pytest (infra, 19)  →  cdk synth (all stacks)  →  docker build (Dockerfile + Dockerfile.ingest)
```
Each step required green. `cdk synth` needs no credentials; `docker build` validates both images build (catches Dockerfile/dep breakage early). Runs on Python 3.12 (repo pins `>=3.11`); installs the package with `uv` and infra deps from `infra/requirements.txt`.

**`deploy.yml`** — on `push` of a tag matching `v*`, and `workflow_dispatch` (for rollback). Permissions `id-token: write` (OIDC) + `contents: read`.
```
checkout (the tag) → assume OIDC deploy role (aws-actions/configure-aws-credentials)
→ cdk deploy PepOracleProdStack PepOracleIngestStack \
     -c git_sha=$GITHUB_SHA_SHORT -c semver=$TAG -c allowed_email=${{ vars.ALLOWED_EMAIL }}
→ post-deploy smoke test vs https://pep-oracle.iicapn.com
```
`cdk deploy` builds + pushes the images via `from_asset` and updates the Lambda + Fargate task def. On `workflow_dispatch`, the operator selects a prior `v*` tag in the Actions "Run workflow" tag dropdown → same job redeploys that tag (image already in ECR → ~1–2 min Lambda update).

### One-time OIDC bootstrap — `PepOracleCicdStack` (new, ap-southeast-2)

Deployed **manually once** with admin creds (the pipeline cannot create its own trust). Contents:
- A GitHub OIDC identity provider for `token.actions.githubusercontent.com` (audience `sts.amazonaws.com`).
- A **deploy role** whose trust policy restricts `sub` to `repo:greggkp/pep-oracle:ref:refs/tags/v*` (only `v*` refs can assume it — a tag push or a `workflow_dispatch` run on a tag ref). Its permissions are minimal: `sts:AssumeRole` on the CDK bootstrap roles `arn:aws:iam::940831808393:role/cdk-hnb659fds-{deploy,file-publishing,image-publishing,lookup}-role-*` in **ap-southeast-2 and us-east-1** (us-east-1 is needed because `PepOracleProdStack` reads the cross-region cert export from `PepOracleCertStack` via a custom resource). CDK does the actual resource mutations through those bootstrap roles, so the deploy role itself needs nothing broader.

GitHub stores only the **role ARN** and **allowed email** as repo *variables* (not secrets) — zero long-lived AWS credentials anywhere.

### App change — version traceability (`semver`)

Add a `semver` field to `DeployConfig` (context `-c semver=`), thread it to the serving Lambda env `PEP_ORACLE_SEMVER`, and surface it in `GET /version` alongside the existing `code_git_sha`. A release tag is then visible end-to-end: `git tag v1.0.0` → `/version` reports `"semver": "v1.0.0"`. Default `"unknown"` when unset (local/manual deploys unaffected). One-line additions in `infra/pep_oracle_infra/config.py`, `infra/pep_oracle_infra/prod_stack.py`, `src/pep_oracle/server.py`.

### Post-deploy smoke (auth-free)

A standalone script `scripts/smoke.py` (no pytest fixtures, so the deploy job just runs `python scripts/smoke.py`) the deploy job runs after `cdk deploy`, polling `https://pep-oracle.iicapn.com` (override via `PEP_ORACLE_SMOKE_URL`):
- `GET /health` → 200
- `GET /version` → `code_git_sha` == the deployed short SHA **and** `semver` == the tag (proves the new image is actually live, not a stale warm container)
- `GET /.well-known/oauth-authorization-server` → 200
- `POST /mcp` with no token → 401

Any failure fails the workflow (red ✗ on the release). It does **not** mint a JWT, so the CI role needs no `ssm:GetParameter` on the signing key. (`test_smoke_live.py`'s minted-JWT path stays for local/manual use.)

### Rollback

Tags are immutable, so rollback is "deploy the previous good tag": GitHub Actions → `deploy.yml` → Run workflow → pick the prior `v*` tag. The OIDC trust (`refs/tags/v*`) allows it; the prior image is already in ECR, so it's a fast Lambda/task-def update. No git history rewrite, no rebuild from scratch.

## Files

**New**
- `.github/workflows/ci.yml`
- `.github/workflows/deploy.yml`
- `infra/pep_oracle_infra/cicd_stack.py` — `PepOracleCicdStack` (OIDC provider + deploy role)
- `infra/tests/test_cicd_stack.py` — assertions: OIDC provider, trust restricted to `refs/tags/v*` + the repo, `sts:AssumeRole` scoped to `cdk-hnb659fds-*`
- `scripts/smoke.py` — the auth-free smoke checks (run as `python scripts/smoke.py`)
- `docs/aws/phase4-cicd-runbook.md` — one-time bootstrap, set repo variables, first release, rollback

**Modified**
- `infra/pep_oracle_infra/config.py` — add `semver` field + context read
- `infra/pep_oracle_infra/prod_stack.py` — add `PEP_ORACLE_SEMVER` to the Lambda env
- `src/pep_oracle/server.py` — add `semver` to the `/version` payload
- `infra/app.py` — instantiate `PepOracleCicdStack`
- `pyproject.toml` — `[tool.ruff]` lenient config + a `dev` optional-dependency group with `ruff`
- `CLAUDE.md` — Phase 4 bullet

## Testing

- **CI gate is itself the test** for the workflows (a PR exercises `ci.yml`; a throwaway pre-release tag exercises `deploy.yml`).
- **CDK assertion tests** (`infra/tests/test_cicd_stack.py`): OIDC provider present; deploy-role trust `Condition` pins `token.actions.githubusercontent.com:sub` to `repo:greggkp/pep-oracle:ref:refs/tags/v*` and `:aud` to `sts.amazonaws.com`; role policy grants only `sts:AssumeRole` on `cdk-hnb659fds-*`.
- **ruff** runs clean after the one-time cleanup; added to the gate so it stays clean.
- **Smoke** validated on the first real `v*` release against the live endpoint (`/version` semver advances).
- The existing root (387) + infra (19) suites must stay green with the `semver` + ruff changes.

## Security

- **No long-lived AWS credentials in GitHub** — OIDC exchanges a per-job token for short-lived STS creds; trust is scoped to the repo + `v*` tag refs only.
- **Least-privilege deploy role** — it can only assume the CDK bootstrap roles; all mutations flow through CDK's own scoped roles. It cannot read the OAuth signing key, the corpus, or DynamoDB.
- **Auth-free smoke** keeps the signing key out of CI entirely.
- Repo *variables* (role ARN, allowed email) are non-secret; no secrets are required for the pipeline.

## Out of scope (deferred / later phases)

- **Staging environment + promote-by-digest** (original sketch) — YAGNI for one operator; can be added later by parameterizing the stacks on an env name.
- **Lambda alias / version-based instant rollback** — redeploy-prior-tag is sufficient at this scale.
- **`PepOracleCertStack` in the pipeline** — manual, rare, cross-region.
- **The corpus artifact / backfill** — data, not code; stays a manual one-shot (Phase 3 runbook).
- **KMS asymmetric JWT signing** — Phase 5 (the `signing.py` seam is the hook).
- **Deferred perf** — Lambda cold-start lazy-import + ingest image de-bloat (separate tech-debt items).
