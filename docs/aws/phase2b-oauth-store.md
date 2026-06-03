# Phase 2b — OAuth state in DynamoDB

The OAuth provider's state (registered clients, single-use auth codes, refresh
tokens) lives behind `oauth_store.OAuthStore`. `PEP_ORACLE_OAUTH_STORE` selects the
backend: `sqlite` (local default, `~/.pep-oracle/oauth.db`) or `dynamodb` (the
Lambda). Auth codes moved out of the in-process `_auth_codes` dict into the store so
any stateless Lambda container can complete an authorize→token exchange.

## Why DynamoDB for the Lambda
Refresh rotation must be race-safe across concurrent containers. `revoke_refresh`
is a conditional write (`revoked = 0` guard) returning won/lost; on `/oauth/token`
refresh, the winner issues the new pair and the loser gets a clean 400 — **no**
spurious family revocation. Genuine reuse (a token already revoked at read time)
still revokes the whole family (RFC 9700 §4.13.2).

## DynamoDB table (provisioned by CDK in Phase 2c)
Single table, `pk` = `client#…` / `code#…` / `refresh#…`, a `family-index` GSI on
`family_id` (for family revocation), and native `ttl` (cleanup only — reads still
check `expires_at`). `DynamoDbStore.ensure_table()` creates it for local/moto; prod
comes from CDK.

## Local
Default is SQLite — nothing to run. The DynamoDB path is covered by the contract
tests (`tests/test_oauth_store.py`, moto) which run every behavior against BOTH
backends, so SQLite and DynamoDB are held to one spec.

## Out of scope (Phase 2b2 / 2c)
JWT signing seam (HS256 from SSM) + Cognito gate on `/oauth/authorize` are **2b2**.
The real DynamoDB table + IAM are **2c**.
