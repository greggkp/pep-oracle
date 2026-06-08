# Phase 4 — CI/CD runbook (GitHub OIDC, tag-to-deploy)

Account 940831808393, region ap-southeast-2, repo greggkp/pep-oracle.
CDK CLI local: `cd infra && PATH="$PWD/.venv/bin:$PATH" ./node_modules/.bin/cdk ...`.

## One-time bootstrap (admin creds, once ever)
1. Deploy the OIDC + deploy-role stack:
   ```bash
   cd infra
   PATH="$PWD/.venv/bin:$PATH" ./node_modules/.bin/cdk deploy PepOracleCicdStack --require-approval never
   ```
   Note the `DeployRoleArn` output (e.g. `arn:aws:iam::940831808393:role/pep-oracle-github-deploy`).
2. Set GitHub repo **variables** (Settings → Secrets and variables → Actions → **Variables**, not Secrets — neither value is sensitive):
   - `AWS_DEPLOY_ROLE_ARN` = the role ARN from step 1
   - `ALLOWED_EMAIL` = `greggkp71@gmail.com`

## Normal release
```bash
git tag v1.0.0
git push origin v1.0.0
```
`deploy.yml` builds the images (`from_asset`), deploys `PepOracleProdStack` + `PepOracleIngestStack`
via the OIDC role, then runs `scripts/smoke.py` against https://pep-oracle.iicapn.com. The tag shows
in `GET /version` as `code_semver`, and `code_git_sha` is the tagged commit. A red ✗ = the smoke
failed; the previous version is still live (Lambda update is atomic — a failed smoke means the new
code is live but didn't pass, so roll back).

## Rollback
GitHub → Actions → **deploy** → Run workflow → in the ref dropdown **pick the previous `v*` tag**.
The OIDC trust allows any `v*` ref; the prior image is already in ECR, so it's a ~1–2 min Lambda
update. (Tags are immutable — rollback = redeploy the last good tag.)

**Important:** a `workflow_dispatch` run only obtains AWS credentials when run against a **`v*` tag
ref** — the OIDC trust subject is `repo:greggkp/pep-oracle:ref:refs/tags/v*`. Dispatching against
`main` (or any branch) fails at the assume-role step with an STS denial. Always select a tag.

## CI gate (no action needed — runs automatically)
`ci.yml` runs on every PR and every push to `main`: `ruff check` + root `pytest` (via `uv`) + infra
`pytest` + `cdk synth --all` + a docker build of both images. No AWS access (no OIDC, no creds). A
red CI blocks the merge; fix before tagging a release.

## Notes
- `PepOracleCertStack` is **not** in the pipeline (manual, rare, us-east-1).
- If a deploy ever fails on a bootstrap-version lookup, confirm the deploy role's `ssm:GetParameter`
  on `/cdk-bootstrap/hnb659fds/version` (granted by `PepOracleCicdStack`).
- The corpus artifact is data, not code — never deployed by this pipeline (see the Phase 3 runbook).
- The CDK CLI is pinned to `2.1126.0` in both workflows (matches `infra/package.json`); bump both
  together if upgrading.
