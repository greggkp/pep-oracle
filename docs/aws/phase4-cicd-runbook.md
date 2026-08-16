# Release and rollback runbook

Account 940831808393, region ap-southeast-2, repo greggkp/pep-oracle.
CDK CLI local: `cd infra && PATH="$PWD/.venv/bin:$PATH" ./node_modules/.bin/cdk ...`.

## Normal release

The daily release train (`release-train.yml`) checks for undeployed commits at 06:00
UTC. When changes exist, it chooses the next patch version and dispatches
`deploy.yml` from `main`. The production GitHub Environment may pause the run for
approval. A successful run deploys both application stacks, smoke-tests
`https://pep-oracle.iicapn.com`, and creates the annotated release tag.

For an immediate or explicitly versioned release, open GitHub → Actions → **deploy**
→ **Run workflow**, select `main`, and enter an unused `vMAJOR.MINOR.PATCH` version.
Manual dispatch from `main` is supported: the deploy job declares the `production`
Environment, whose OIDC subject is trusted by the AWS role.

A direct push of a new `v*` tag remains supported, but the automated or manual
workflow-dispatch paths are preferred because they deploy before creating the tag.

`deploy.yml` builds the images, deploys `PepOracleProdStack` and
`PepOracleIngestStack`, then runs `scripts/smoke.py`. `GET /version` must report the
requested semantic version and deployed commit. Treat a failed smoke test as a
failed release: CloudFormation may already have activated the new Lambda version,
so investigate or roll back promptly.

## Rollback

GitHub → Actions → **deploy** → **Run workflow**. Select the last known-good `v*`
tag in the ref dropdown and enter that same existing version. The workflow redeploys
the tagged commit and skips tag creation because the tag already exists. The image
assets are normally already in ECR, making this faster than a new release.

Do not move or recreate release tags. Rollback means redeploying an immutable prior
tag, then confirming the smoke job and `GET /version`.

## CI gate

Every PR and push to `main` runs secret scanning, Ruff lint and format checks, Mypy,
root and infrastructure tests, CDK synth, both Docker builds, pip-audit, and blocking
high/critical Trivy scans. GitHub CodeQL default setup covers Python and Actions. A
red required check blocks merge and must be fixed before release.

## One-time bootstrap

1. Deploy the OIDC + deploy-role stack:
   ```bash
   cd infra
   PATH="$PWD/.venv/bin:$PATH" ./node_modules/.bin/cdk deploy PepOracleCicdStack --require-approval never
   ```
   Note the `DeployRoleArn` output (e.g. `arn:aws:iam::940831808393:role/pep-oracle-github-deploy`).
2. Set GitHub repo **variables** (Settings → Secrets and variables → Actions →
   **Variables**, not Secrets — neither value is sensitive):
   - `AWS_DEPLOY_ROLE_ARN` = the role ARN from step 1
   - `ALLOWED_EMAIL` = `greggkp71@gmail.com`

## Notes

- If a deploy ever fails on a bootstrap-version lookup, confirm the deploy role's `ssm:GetParameter`
  on `/cdk-bootstrap/hnb659fds/version` (granted by `PepOracleCicdStack`).
- The deploy role trusts both `v*` tag refs and the `production` Environment subject;
  do not broaden it to arbitrary branch subjects.
- `PepOracleCertStack` remains manual because it is rarely changed and deploys in
  us-east-1.
- The corpus artifact is data, not code and is published by the ingestion task.
- Keep the CDK CLI version in workflows aligned with `infra/package.json`.
