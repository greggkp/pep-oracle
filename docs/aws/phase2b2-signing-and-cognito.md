# Phase 2b2 — Signing backend + Cognito authorize gate (operator runbook)

Two app seams for the AWS serving Lambda. Both default to the OptiPlex behavior;
opt in with env vars. The real AWS resources below are provisioned by the Phase 2c
CDK — these manual steps let you smoke-test the app against real AWS first.

## Signing key: HS256 from SSM SecureString

Select with `PEP_ORACLE_OAUTH_SIGNING_BACKEND=ssm`. Create the parameter once:

```bash
KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
aws ssm put-parameter \
  --name /pep-oracle/oauth-signing-key \
  --type SecureString \
  --value "$KEY" \
  --region ap-southeast-2
```

Env:
- `PEP_ORACLE_OAUTH_SIGNING_BACKEND=ssm`
- `PEP_ORACLE_OAUTH_SIGNING_SSM_PARAM=/pep-oracle/oauth-signing-key` (default)
- `PEP_ORACLE_OAUTH_SIGNING_SSM_REGION=ap-southeast-2` (defaults to the Bedrock region)

The Lambda's IAM role needs `ssm:GetParameter` on that one parameter ARN (Phase 2c).
A missing/empty parameter makes the app raise on startup (fail-closed) — it never
silently generates a key that would invalidate every previously issued token.

## Authorize gate: one-user Cognito pool

Select with `PEP_ORACLE_AUTHORIZE_GATE=cognito`. One-time setup (CDK does this in 2c):

1. Create a user pool (email sign-in; email OTP is enough for one user). Note the
   pool id, e.g. `ap-southeast-2_abc123`.
2. Add a Hosted UI **domain**, e.g. `pep-oracle` →
   `https://pep-oracle.auth.ap-southeast-2.amazoncognito.com`.
3. Create an **app client** *with* a client secret (confidential — server-side token
   exchange). Allowed OAuth flow: Authorization code grant; scopes `openid email`.
   Callback URL: `https://<your-public-url>/oauth/authorize/callback`.
4. Create the single user (your email); set a password / enable email OTP.

Env:
- `PEP_ORACLE_AUTHORIZE_GATE=cognito`
- `PEP_ORACLE_COGNITO_DOMAIN=https://pep-oracle.auth.ap-southeast-2.amazoncognito.com`
- `PEP_ORACLE_COGNITO_CLIENT_ID=<app client id>`
- `PEP_ORACLE_COGNITO_CLIENT_SECRET=<app client secret>`
- `PEP_ORACLE_COGNITO_USER_POOL_ID=ap-southeast-2_abc123`
- `PEP_ORACLE_COGNITO_REGION=ap-southeast-2` (defaults to the Bedrock region)
- `PEP_ORACLE_COGNITO_ALLOWED_EMAILS=you@example.com` (comma-separated allow-list; required)

When `cognito` is selected, `mount_mcp_if_configured` does **not** require
`PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH=1` — the in-app identity check is the auth.
A missing required Cognito var makes mount refuse (fail-closed), as does an
unrecognized `PEP_ORACLE_AUTHORIZE_GATE` value.

## Flow (cognito)

`/oauth/authorize` validates the MCP request, then 302s the browser to the Cognito
Hosted UI carrying a short-lived HS256 "login-state" JWT (the original MCP params).
After login, Cognito redirects to `/oauth/authorize/callback`, which exchanges the
code, verifies the ID token (RS256 via the pool JWKS) and the email allow-list, then
issues the pep-oracle auth code and redirects back to the MCP client. PKCE survives
the round-trip: the original `code_challenge` rides inside the login-state JWT and is
re-bound to the issued code, so `/oauth/token` still requires the original verifier.
Stateless: no session cookie, no extra store rows. The authorize flow is rare (client
setup), so the round-trip never touches query latency.

## Smoke

```bash
# trusted_upstream (default) — unchanged
uv run pytest -q

# against real AWS: export the env above, then
uv run pep-oracle-server   # check logs say "OAuth provider routes registered" and "MCP mounted at /mcp"
```
