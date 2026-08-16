# Security hardening status

This document records the disposition of the August 2026 security review. It is an
operator checklist, not a substitute for the executable tests and CI checks.

## Implemented controls

- OAuth dynamic registration accepts public PKCE clients only, bounds attacker-
  controlled fields, and is rate-limited at WAF along with `/oauth/token` and `/mcp`.
- Refresh tokens are stored as SHA-256 digests. The temporary legacy plaintext lookup
  preserves tokens issued before the change; every refresh token has a fixed 30-day
  maximum lifetime and new plaintext rows cannot be created.
- The Cognito confidential-client secret is held in CMK-encrypted Secrets Manager,
  retrieved with a secret-specific IAM grant, cached for five minutes, and never put
  in the Lambda environment. Retrieval failure fails the login exchange closed.
- WAF retains only blocked-request logs for 30 days, with query strings and
  `Authorization` redacted. WAF blocks, Lambda errors/throttles, EventBridge delivery
  failures, stopped warming, and API Gateway 5xx responses notify the operator.
- CI pins third-party actions by commit and runs Ruff, Mypy, unit/infrastructure tests,
  CDK synth, two container builds, pip-audit, Gitleaks, and blocking high/critical
  Trivy scans. GitHub CodeQL default setup covers Python and Actions.

## Deployment follow-up

Record the first production deployment date of the refresh-token digest change:

- Deployment date: `YYYY-MM-DD` (fill during the approved deployment)
- Earliest plaintext-fallback removal: deployment date + 30 days

After that date, remove the `(digest, plaintext)` fallback queries/updates and their
legacy tests from `oauth_store.py` and `tests/test_oauth_store.py`, run the complete
security gate, and deploy normally. DynamoDB TTL may delete expired records later
than their logical expiry, but `oauth.py` checks `expires_at` synchronously, so no
expired legacy token can authenticate while physical deletion catches up.

## Evidence-backed decisions

### Dynamic-client record lifetime

Client registration rows intentionally have no TTL. They contain a random public
client ID, display name, and validated redirect URI—not a credential—and every
authorization still requires Cognito allow-list login plus PKCE. Expiring a client
row would break an otherwise active MCP integration without a reliable activity
signal: normal bearer and refresh-token use does not read the client row. Per-IP WAF
registration limits bound the straightforward storage-abuse path. A client TTL is
therefore deferred until the product has either a client-management/revocation path
or a trustworthy last-used signal; adding an arbitrary TTL now would reduce
availability without closing an authentication bypass.

### Additional SAST and IaC scanners

No additional scanner is added in this change. CodeQL, dependency audit, secret
scanning, container scanning, CDK template assertions, and full synth already cover
the current Python/CDK surface. Adding Checkov over synthesized CDK would add a large
dependency/action trust surface and likely require policy suppressions without a
specific uncovered finding. Re-evaluate Checkov or CDK Nag when infrastructure grows,
an organizational baseline requires it, or a concrete misconfiguration demonstrates
a gap. Do not suppress a future finding merely to keep CI green.

### Dependabot alert 34

The default branch requires `h2>=4.4.1` and locks version 4.4.1, the first patched
release for `GHSA-6hr6-w5qg-qmwg` / `CVE-2026-71554` (`h2 <= 4.4.0`). Verify that
Dependabot alert 34 is closed; do not dismiss it as a substitute for the patched lock.
