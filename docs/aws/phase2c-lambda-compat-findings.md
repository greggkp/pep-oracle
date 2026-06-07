# Phase 2c — Lambda app-compat findings (RESOLVED 2026-06-07)

> **STATUS: RESOLVED — the MCP endpoint works end-to-end on AWS.** Verified against the
> CloudFront domain: `/health`, `/version` (corpus v0001, eps 169–263), `/oauth` discovery,
> and `/mcp` (no-token→401; minted-JWT `initialize`/`tools/list`/`tools/call`→200 with real
> citations — Bedrock embedding from Lambda + S3-artifact retrieval). Fixes are on branch
> `phase2c-lambda-readiness`. The ONLY remaining step is the DNS cutover of
> `pep-oracle.iicapn.com` (add the 4 Route 53 NS records at Cloudflare + remove the old
> `pep-oracle` record) — user-gated; until then the apex still serves via the tunnel.
>
> Fixes applied: (1) per-request stateless MCP session manager (Mangum runs the ASGI lifespan
> per-invoke; the SDK's `run()` is once-per-instance); (2) artifact-aware `/status`+`/episodes`
> + `DATA_DIR=/tmp` (read-only FS); (2.5, found in smoke) disable the MCP DNS-rebinding
> host-check + normalize `/mcp`→`/mcp/` in the Lambda handler (behind CloudFront→APIGW the
> Lambda sees the execute-api Host → 421 + cross-host 307). (3) the 10s init timeout remains a
> deferred perf follow-up (cold starts are slow but functional; lazy-import chromadb/fastembed).

---

The Phase 2c CDK stack deployed successfully to AWS (account 940831808393, ap-southeast-2),
but **running the actual app on Lambda surfaced real app-level compat bugs** — Phase 2a added
the Mangum handler but the app was never run on real Lambda (unit tests mock the server). The
original findings below are kept for the record; all but the perf item (#3) are now fixed.

## Deployed state (live, working at the infra layer)
- Stacks: `PepOracleCertStack` (us-east-1, ACM cert ISSUED via a Cloudflare validation CNAME),
  `PepOracleProdStack` (ap-southeast-2) — both `CREATE/UPDATE_COMPLETE`.
- CloudFront: `d2bokiv7w0sgrv.cloudfront.net` (Deployed) → **API Gateway HTTP API** → Lambda.
- Lambda: container image (`pep_oracle.server.handler`), Active; serves from S3 corpus artifact.
- Data: S3 `pep-oracle-corpus-prod` (corpus v0001 uploaded), DynamoDB `pep-oracle-oauth`,
  KMS DataKey, SSM SecureString `/pep-oracle/oauth-signing-key` (v1), Cognito pool
  `ap-southeast-2_OilXN3JZy` + user `greggkp71@gmail.com`.
- `pep-oracle.iicapn.com` is **NOT** cut over — only the Cloudflare validation CNAME was added;
  the apex still points at the tunnel. NS delegation (the 4 Route 53 NS records) is **not** done.

## The blocking app bugs (CloudWatch, in severity order)

### 1. CRITICAL — MCP StreamableHTTPSessionManager vs Mangum lifespan (all routes 500)
`RuntimeError: StreamableHTTPSessionManager .run() can only be called once per instance` →
`mangum ... LifespanFailure`. `server.mount_mcp_if_configured` wraps the app lifespan with
`session_manager.run()`. Mangum runs the ASGI lifespan **per invocation**, but the MCP session
manager's `run()` is once-per-instance → the 2nd+ request fails lifespan startup → every route
500s. The MCP streamable-HTTP session manager assumes a long-running ASGI server, not Lambda's
per-invoke model.
**Fix direction:** don't drive the session manager via the per-invoke lifespan on Lambda. Options
to evaluate: (a) Mangum `lifespan="off"` + start the session manager once at container init
(module load) guarded so it runs once per container; (b) use the MCP SDK's stateless request
handling path directly (we already set `stateless_http=True`) without the long-lived task group;
(c) construct a fresh session manager per invocation. Needs a Lambda-shaped test (invoke the
Mangum handler twice in one process and assert both succeed).

### 2. Read-only filesystem
`OSError: [Errno 30] Read-only file system: '/home/sbx_user1051'` — `config.ensure_dirs()`
`mkdir`s under `~/.pep-oracle` (HOME is read-only on Lambda; only `/tmp` is writable), and
`/status` → `_fetch_status` → `_get_fresh_collection()` calls **ChromaDB** even though
`SERVE_FROM_ARTIFACT=1`.
**Fix direction:** set `PEP_ORACLE_DATA_DIR=/tmp/.pep-oracle` in the Lambda env (CDK), AND make
`/status` (and any `ensure_dirs`/ChromaDB call) respect the artifact seam so the artifact serve
path never touches ChromaDB/disk. Ideally `ensure_dirs()` is not called at all on the artifact
path.

### 3. Init 10s timeout
`INIT_REPORT ... Phase: init Status: timeout` — module import (chromadb/fastembed/etc.) exceeds
the 10s Lambda init phase. Works (init falls into the invoke) but slows cold starts.
**Fix direction:** lazy-import the heavy/unused-on-serve deps (chromadb, fastembed) so the
artifact serve path imports light; consider raising memory (more CPU) for faster cold start.

## Deploy-discovered infra fixes already applied (branch `phase2c-deploy-fixes`)
- `lambda_reserved_concurrency` made optional (default 0) — the account's Lambda concurrency
  limit is **10**, so any reservation fails (`UnreservedConcurrentExecution below minimum 10`).
- **Public (auth=NONE) Lambda Function URLs are blocked account-wide** (confirmed via a throwaway
  function — both 403 with correct public-invoke policy, no SCP). Pivoted CloudFront → **API
  Gateway HTTP API** ($default proxy → Lambda): not a function URL (not blocked), passes the
  bearer natively (no OAC/SigV4 conflict), Mangum handles APIGW v2 events with no app change.

## Cleanup owed (from the failed first deploy + live debugging)
- Orphaned from the first (rolled-back) deploy attempt: an extra KMS key (~$1/mo) and a Cognito
  user pool (no domain). Identify by created-at / not-referenced-by-the-live-stack before deleting.
- A manual `PublicUrlInvokeManual` Lambda permission was added during debugging; the function URL
  is gone now (APIGW), so it's moot drift — remove it.

## Suggested next task
Make the app Lambda-ready (issues 1–3) with tests, then resume the deploy: set DATA_DIR in CDK,
redeploy, smoke-test `/version` + `/mcp` (minted JWT) + `/oauth/*` on the CloudFront domain, then
do the NS cutover (the 4 Route 53 NS records at Cloudflare + remove the old `pep-oracle` record).
Note the `/mcp` Host-header check (MCP allow-list vs the APIGW execute-api Host) is still untested
behind the 500s — verify it once issue 1 is fixed.
