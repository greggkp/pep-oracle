# Phase 2c — Deploy & cutover runbook (pep-oracle prod serving stack)

Authored by the Phase 2c plan; **execute only after explicit go-ahead**. Provisions
real, billable resources (≈$2-4/mo idle) and performs a live DNS cutover of
pep-oracle.iicapn.com. Region: ap-southeast-2 (compute) + us-east-1 (CloudFront cert).
AWS profile: the OptiPlex default (e.g. `optiplex-cli`, account 940831808393).

CDK app lives in `infra/`. It pins `aws-cdk-lib==2.180.0` (the version providing
`FunctionUrlOrigin.with_origin_access_control`).

## 0. Prereqs
- Node + `npm i -g aws-cdk` (the CDK CLI); Docker running (the Lambda image builds at deploy).
- `cd infra && python -m venv .venv && .venv/bin/pip install -r requirements.txt`
- Confirm Bedrock Titan v2 access in ap-southeast-2 (already verified for this account).

## 1. Bootstrap both regions (one-time per account)
```bash
cd infra
.venv/bin/cdk bootstrap aws://<ACCOUNT_ID>/ap-southeast-2 aws://<ACCOUNT_ID>/us-east-1
```

## 2. Deploy the cert/zone stack first (creates the Route 53 hosted zone)
```bash
.venv/bin/cdk deploy PepOracleCertStack -c allowed_email=<you@example.com>
```
Then **delegate the subdomain**: read the 4 NS records of the new `pep-oracle.iicapn.com`
hosted zone (Route 53 console or `aws route53 get-hosted-zone`), and at the parent
`iicapn.com` DNS (currently Cloudflare) add an `NS` record for `pep-oracle` pointing at
those 4 values. ACM DNS-validation completes automatically once delegation propagates
(the validation CNAME lives in the new zone). Wait for the cert to reach ISSUED.

Also confirm the cross-region cert reference wires before the prod deploy:
```bash
.venv/bin/cdk synth PepOracleProdStack -c allowed_email=<you@example.com> >/dev/null && echo "synth OK"
```
If synth errors on the cross-region cert/zone reference, switch `prod_stack.py` to accept
the `acm.ICertificate` / `route53.IHostedZone` constructs directly (pass `cert_stack.certificate`
/ `cert_stack.hosted_zone` from `app.py`) per the plan's Task 7 "Cross-region reference" note.

## 3. Deploy the prod stack (builds + pushes the Lambda image)
```bash
.venv/bin/cdk deploy PepOracleProdStack -c allowed_email=<you@example.com>
```
Note the stack outputs (KMS DataKey id, Cognito user-pool id, CloudFront domain).

## 4. Create the SSM SecureString signing key (encrypted with the stack KMS key)
The CDK grants the Lambda `ssm:GetParameter` + `kms:Decrypt`; create the value out-of-band
AFTER the prod stack exists (so the KMS key id is known):
```bash
KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
aws ssm put-parameter --name /pep-oracle/oauth-signing-key --type SecureString \
  --value "$KEY" --key-id <DataKey-id-from-outputs> --region ap-southeast-2
```
A missing param makes the OAuth path fail closed — create it before smoke-testing OAuth.

## 5. Create the single Cognito user
```bash
aws cognito-idp admin-create-user --user-pool-id <pool-id-from-outputs> \
  --username <you@example.com> --user-attributes Name=email,Value=<you@example.com> \
    Name=email_verified,Value=true --region ap-southeast-2
# set a permanent password:
aws cognito-idp admin-set-user-password --user-pool-id <pool-id> \
  --username <you@example.com> --password '<strong-pw>' --permanent --region ap-southeast-2
```

## 6. Publish the corpus artifact to S3
The local artifact is at `~/.pep-oracle/corpus/` (v0001, Titan v2). Upload preserving the
`corpus/` prefix the loader expects (`s3://<bucket>/corpus/{vNNNN.parquet,...,current.json}`):
```bash
aws s3 sync ~/.pep-oracle/corpus/ s3://pep-oracle-corpus-prod/corpus/ --region ap-southeast-2
```

## 7. Smoke test BEFORE cutover (CloudFront default domain)
The distribution has a `*.cloudfront.net` domain (stack output). Point the smoke test at it:
```bash
PEP_ORACLE_SMOKE_URL=https://<dxxxx>.cloudfront.net uv run pytest -m live tests/test_smoke_live.py -q
curl -s https://<dxxxx>.cloudfront.net/version | jq    # code + corpus versions
```
Confirm `/mcp` rejects no-token and accepts a minted JWT; `/version` reports corpus v0001.

## 8. Direct cutover of pep-oracle.iicapn.com
The A-alias in the prod stack points the apex of the delegated zone at CloudFront, so once
the NS delegation (step 2) is live, `https://pep-oracle.iicapn.com` resolves to the new
stack. **Cutover = the moment delegation propagates.** To switch from the existing
Cloudflare-tunnel/OptiPlex endpoint:
1. Confirm step 7 passed against the CloudFront domain.
2. Ensure the `pep-oracle` NS delegation at iicapn.com is the authority (remove the old
   Cloudflare tunnel CNAME/record for `pep-oracle` if it conflicts).
3. Verify: `dig pep-oracle.iicapn.com` → CloudFront; `curl https://pep-oracle.iicapn.com/version`.
4. Existing MCP client registrations are preserved (same issuer URL / PUBLIC_URL).

**Rollback:** restore the prior Cloudflare record for `pep-oracle` (revert the NS delegation);
the OptiPlex endpoint resumes. DNS TTLs govern propagation.

## 9. Stop the OptiPlex serving (optional, after a soak)
Once stable, disable `pep-oracle-api.service` + the Cloudflare tunnel for `/mcp` (keep
ingestion until Phase 3 moves it to Fargate).

## Notes / future hardening
- Cognito app-client secret currently lands in the Lambda env (`unsafe_unwrap`). For a
  single-user box this is acceptable; a later hardening moves it to SSM SecureString and
  reads it in `config.py` (a new signing-style seam).
- KMS asymmetric JWT signing = Phase 5 (swap the `signing.py` backend; the seam exists).
- Staging environment + GitHub OIDC promote-by-digest pipeline = Phase 4.
