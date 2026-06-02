# AWS Migration Design — pep-oracle MCP endpoint

**Date:** 2026-06-02
**Scope:** Migrate the MCP serving endpoint (and the ingestion that feeds it) from the
local OptiPlex/Cloudflare deployment to AWS. The `/ask` web UI path is **out of scope**.

## Goals

1. **≈$0 cost when idle** — scale to zero between requests.
2. **Securely accessed from a public endpoint.**
3. **GitHub-based CI/CD pipeline.**
4. **Split ingestion from MCP serving** — independent halves.
5. **Run the interacting components locally** (laptop experimentation) *and* in CI.
6. **Proper release process** — always know which code + data version is in production.
7. **Keys handled as securely as possible.**
8. **Responsive** MCP query path.
9. **Multiple concurrent users.**

## Key facts that shaped the design

- Corpus is tiny: **142 MB ChromaDB, ≈10k chunks, episodes 169–263** (95 episodes), plus a
  20 KB OAuth SQLite DB and small JSON files.
- The only heavy thing on the request path is the **1.3 GB `bge-large` ONNX model**
  (`embeddings.py`), loaded as a lazy singleton to embed the *query* at request time
  (`mcp_server.py:99`). Query and corpus vectors must share one vector space, so the
  embedder is not swappable without re-embedding the corpus.
- BM25 (`lexical.BM25`) has no model — pure Python, built in-memory from corpus text.
- The MCP tool **never calls Claude** — it only embeds + retrieves. No `ANTHROPIC_API_KEY`
  on the serve path. Ingestion (chunk→embed→store, GPU via Modal) doesn't call Claude
  either; the Haiku preprocessor lives in the `/ask` path, which is out of scope.

## Decisions (the forks we resolved)

| Decision | Choice |
|---|---|
| Query/corpus embedding | **Drop `bge-large`; embed via AWS Bedrock** (Titan v2 default, validated against Cohere via the eval harness) |
| GPU transcription/diarization | **Keep Modal**; AWS orchestrates ingestion and calls Modal as today |
| Serve-side vector store | **Flat artifact in S3, loaded into memory** (drop ChromaDB on the serve side) |
| Serving compute | **Lambda (container image), FastAPI + Mangum**, scale to zero |
| OAuth state | **DynamoDB** (on-demand), conditional-write token rotation |
| IaC | **AWS CDK (Python)**, two stacks: staging + prod |
| `/oauth/authorize` gate | **Replace auto-approve with a Cognito identity check** (one-user pool, OTP/passkey) |
| JWT signing | **Pluggable backend, phased**: Phase 1 HS256 from SSM SecureString; Phase 2 KMS-managed asymmetric key (ES256/RS256), private key never leaves KMS |

---

## Section 1 — Topology & serving compute

Two fully decoupled halves sharing only a versioned **corpus artifact** in S3.

```
┌─────────── INGESTION (episodic, AWS-orchestrated) ───────────┐
│  EventBridge cron → Fargate task (scale-to-zero)             │
│    ├─ feed.py: find new episodes                            │
│    ├─ Modal: whisper + pyannote  (unchanged GPU backend)    │
│    ├─ chunk + embed via Bedrock (Titan v2, 1024-dim)        │
│    └─ publish corpus-vN.parquet + manifest → S3 (atomic)    │
└──────────────────────────────────────────────────────────────┘
                          │  (S3 artifact only — no shared DB)
                          ▼
┌─────────── SERVING (scale-to-zero, responsive) ──────────────┐
│  CloudFront+OAC → Lambda (FastAPI + Mangum), same app local  │
│    ├─ cold start: pull corpus-vN.parquet from S3 → memory   │
│    ├─ query: Bedrock embed → in-mem cosine + BM25(RRF)      │
│    │         → temporal rerank                              │
│    └─ OAuth 2.1 / DCR state in DynamoDB (TTL on codes/toks) │
└──────────────────────────────────────────────────────────────┘
```

**Serving runtime — the parity move.** Keep `server.py` as a normal FastAPI app and wrap
it with **Mangum** (ASGI→Lambda adapter). The *same* ASGI app runs three ways: `uvicorn`
locally, under pytest in CI, and via Mangum in Lambda. No "works locally, breaks in Lambda"
drift.

**Why Lambda, not Fargate, for serving.** With `bge-large` gone there is nothing heavy on
the request path — Bedrock embeds over the network, retrieval is in-memory over 10k vectors
(sub-millisecond). Cold start is "pull 142 MB from S3 + JWT verify," comfortably within the
responsiveness bar, and idle cost is $0. Fargate would impose a monthly floor for no benefit.

**Concurrency (multiple users) — three load-bearing points:**
1. **MCP must run stateless.** Instantiate FastMCP with `stateless_http=True` so every
   request is self-contained (JSON, no long-lived server→client SSE, no per-session state).
   Any Lambda container can serve any request — required for horizontal scale with no sticky
   sessions.
2. **OAuth token rotation must be concurrency-safe.** Refresh rotation + family revocation
   becomes a DynamoDB **conditional write** (`ConditionExpression` on current token state):
   exactly one rotation wins, the loser gets a clean 400. Prevents races that would wrongly
   revoke a family or mint two live tokens.
3. **Per-container corpus copies.** Each concurrent cold container independently loads the
   142 MB artifact from S3 (S3 absorbs the fan-out). In-memory copies mean zero cross-request
   contention on the hot path. Guard with a **reserved-concurrency cap** (e.g. 20–50) so a
   misbehaving client can't fan out unboundedly into Bedrock. Cold-start-on-burst is the one
   honest tradeoff vs. truly-zero-idle; default to scale-to-zero and add an EventBridge
   keep-warm ping or small provisioned concurrency only if real usage demands it.

---

## Section 2 — Corpus artifact & ingestion data flow

The corpus is a **versioned, immutable release artifact**, not a live database.

**Artifact shape (one immutable set of objects per ingest run):**
```
s3://pep-oracle-corpus/
  corpus/v0042.parquet         # vectors(float32,1024) + chunk text + metadata, one row/chunk
  corpus/v0042.manifest.json   # {schema_ver, embed_model, dims, episode_range, chunk_count,
                               #  ingest_git_sha, built_at, sha256}
  corpus/current.json          # {version, sha256, manifest_url}  ← the only mutable object
```
Parquet: columnar, compresses vectors well, one-read load into numpy/Arrow, locally
inspectable. Vectors load into an `(N,1024)` array for brute-force cosine; text + metadata
ride alongside for BM25 and citation formatting.

**Publish = write-then-flip (atomic).** Write `vNNNN.parquet` (immutable key) → verify
sha256 → write manifest → *last*, overwrite the tiny `current.json`. S3 PUT is atomic with
read-after-write consistency, so readers see old or new, never a half-written corpus.
In-flight servers keep their loaded copy; nothing mutates under them.

**Two independent version axes** (this answers "what's running in prod"):
- **Code version** — serving Lambda image (git SHA + semver), controlled by the release
  pipeline.
- **Corpus version** — the data artifact (`vNNNN`), updated by ingestion **without
  redeploying serving**.

Coupling them would force a serving redeploy per new episode — the coupling we are avoiding.
`GET /version` reports both:
```json
{ "code_semver": "1.4.0", "code_git_sha": "a7a1fb7", "corpus_version": "v0042",
  "corpus_episode_range": [169, 264], "corpus_built_at": "2026-06-01T06:14:00Z" }
```

**Picking up new corpus (bounded staleness).** A cold container reads `current.json` →
downloads that exact version → loads to memory → records the version. Warm containers
re-check `current.json` on a short TTL (default 5 min, a cheap small-object GET); on change
they load the new artifact into a fresh structure and **atomically swap the reference**
(no lock on the read path). New episodes reach live serving within ≈5 min, no deploy, and
`/version` never lies. `PEP_ORACLE_CORPUS_VERSION=v0042` can pin a container for strict
reproducibility.

**Local / CI / prod parity.** All three use the identical loader; `PEP_ORACLE_CORPUS_URI`
(default prod bucket) selects the source — local file or dev bucket locally, small fixture
in CI, `current.json` in prod.

**One-time migration (cheap, no Modal/GPU).** The 95 existing episodes already have
transcripts and chunks. A backfill job reads the `pep-oracle export` JSON, **re-embeds the
chunk text via Bedrock** (discarding old `bge-large` vectors), and publishes `v0001.parquet`.
Transcription/diarization are not re-run — one Bedrock pass over ≈10k short texts, a few cents.

---

## Section 3 — CI/CD, release process & version traceability

**One Dockerfile is the single source of truth for the runtime** — same image in CI, runnable
locally, *and* the Lambda (container-image Lambda, not a zip). Local laptop dev can still be
bare `uvicorn` for speed, but the authoritative runtime is the image.

**Keyless AWS auth.** GitHub Actions uses **OIDC federation** — a per-environment IAM deploy
role whose trust policy is scoped to the repo and the right ref (`main` / `v*` tags). GitHub
stores zero AWS secrets; it exchanges its OIDC token for short-lived STS creds at job time.

**IaC: AWS CDK (Python).** Two stacks — `pep-oracle-staging`, `pep-oracle-prod` — each owning
its Lambda, DynamoDB table, S3 prefix, KMS key, Cognito pool, and IAM. Both scale to zero, so
two environments still cost ≈$0 idle.

**Pipeline (three triggers, promote-by-digest):**
```
PR opened/updated → ruff + pytest (289 tests) + cdk synth + docker build  (no deploy)
push to main      → build image tagged with git SHA → push to ECR
                  → cdk deploy STAGING (image by digest)
                  → live smoke test vs staging (test_smoke_live.py, PEP_ORACLE_SMOKE_URL)
git tag vX.Y.Z    → PROMOTE THE SAME IMAGE DIGEST to PROD (no rebuild)
                  → cdk deploy PROD → smoke test prod → GitHub Release
```
Critical property: **prod runs the byte-identical image that passed staging** (tag promotes
the existing ECR digest, no rebuild).

**Version traceability.** Build bakes `git_sha` + `semver` into the image; combined with the
corpus version, `GET /version` returns the full truth. **Rollback is instant**: prod points a
Lambda alias at a published version (each backed by an ECR digest already present); rolling
back shifts the alias to the prior version — seconds, no rebuild. All-at-once shift is fine at
this scale; canary weighting is available but YAGNI.

The one-time corpus backfill is **not** in this pipeline — it is a manual one-shot invocation
of the ingestion Fargate task during migration.

---

## Section 4 — Security, secrets & the public endpoint

**1. Public endpoint & transport.** **CloudFront → Lambda Function URL**, locked by Origin
Access Control so the Function URL is reachable only via CloudFront. CloudFront provides the
custom domain (ACM cert) + TLS and is the attach point for optional **AWS WAF** (rate-based
rule). FastAPI owns all routing internally (`/mcp`, `/oauth/*`, `/.well-known/*`) — one origin,
no API Gateway route sprawl. MCP runs stateless, so no session affinity is needed.

**2. Secrets — three tiers, decreasing exposure:**
- **Bedrock: zero secrets** — IAM role only. The embedding API key ceases to exist on the
  serve path.
- **No `ANTHROPIC_API_KEY` anywhere in scope** (MCP and ingestion never call Claude). Returns
  only if `/ask` is migrated later.
- **Modal tokens** (ingestion only): SSM Parameter Store **SecureString** (KMS-encrypted),
  injected into the Fargate task via the ECS task-definition `secrets` mechanism — never baked
  into the image.

**3. OAuth signing key — pluggable backend, phased.** `oauth.py` gets a signing-backend
seam selected by config (the same seam local dev already needs for its HS256 dev key), with
two implementations:

- **Phase 1 (first cutover): HS256 from SSM SecureString.** Fewer moving parts during the
  migration. The secret is KMS-encrypted at rest in SSM and never in git. Its one weakness:
  a runtime compromise yields a permanent, offline-forgeable secret you can't prove you've
  contained.
- **Phase 2 (fast-follow, after the migration is stable): KMS-managed asymmetric key
  (ES256/RS256).** The private key never leaves KMS — the Lambda calls `kms:Sign` to mint
  access tokens and verifies locally with the cached public key. Nothing forging-capable sits
  in process memory or env, revocation is a single IAM/KMS action, and every signature is a
  CloudTrail event. Crucially, **only the rare `/oauth/token` mint path calls KMS** — `/mcp`
  verification stays local and sub-millisecond — so query responsiveness is unaffected by the
  upgrade.

Swapping Phase 1 → Phase 2 is contained to the signing seam plus an added `kms:Sign` grant;
no change to token shape or the verify path's latency profile.

**4. IAM least-privilege, per role:**
- *Serving Lambda:* `bedrock:InvokeModel` on the one embed-model ARN; `s3:GetObject` on the
  corpus prefix; DynamoDB RW on the OAuth table; for signing — Phase 1 `ssm:GetParameter` on
  the HS256 secret, Phase 2 `kms:Sign`/`kms:GetPublicKey` on the one signing key. Nothing else.
- *Ingestion Fargate task:* `bedrock:InvokeModel`; `s3:PutObject` on the corpus prefix;
  `ssm:GetParameter` for Modal tokens. No DynamoDB, no KMS-sign.
- *GitHub OIDC deploy roles:* scoped to CDK deploy, separate per environment, assumable only
  from the repo + correct ref.
- **No Lambda in a VPC** — Bedrock/S3/DynamoDB/KMS are IAM-gated regional APIs; staying out of
  a VPC avoids the ENI cold-start penalty. IAM is the trust boundary.

**5. Encryption at rest.** S3 corpus bucket and DynamoDB OAuth table both KMS-encrypted;
bucket private and versioned (a bad corpus publish rolls back like code).

**6. The `/oauth/authorize` gate — Cognito identity check.** Replace the current
"auto-approve behind a trusted edge" with a real identity check: a **one-user Cognito user
pool** (email OTP or passkey). `/oauth/authorize` requires a valid Cognito session before it
approves — fully AWS-native, removes the fail-open-if-misconfigured risk and the external edge
dependency. The authorize flow is rare (client setup, not per query), so its added latency
never touches query responsiveness.

---

## Section 5 — Embedding model, local dev, validation & cost

**Embedding model — decide with the eval harness.** Default **Amazon Titan Text Embeddings
v2 at 1024 dims** (keeps current vector width, cheapest, native). During migration, re-embed
with **Titan v2 and Cohere Embed v3/v4**, run `eval_retrieval.py` (n=29, phrase-grounded) on
each, and pick the winner; confirm no regression vs `bge-large`. The winner is pinned in the
manifest `embed_model`.

**Local-dev parity.** A `PEP_ORACLE_ENV` selector over one codebase:
- **local:** `uvicorn server:app`; corpus from a local `.parquet`; OAuth on **DynamoDB
  Local**; dev HS256 signing key (no KMS on laptop); Bedrock real (your creds) or mocked.
- **ci:** same image; existing Modal mock pattern; Bedrock mocked for unit tests, real (via
  OIDC role) for the staging smoke.
- **prod/staging:** real S3, DynamoDB, KMS signing, Cognito gate.

Ingestion runs the same way — its Fargate container runs locally (`docker run`) with Modal
creds + Bedrock, writing to a dev artifact you inspect before it touches prod.

**Migration validation gate.** (1) Backfill re-embeds 95 episodes → `v0001.parquet`;
(2) eval harness confirms retrieval quality holds; (3) staging smoke test passes against the
real Lambda (`/mcp` rejects no-token, accepts a minted JWT; answers aren't dead-ends);
(4) only then does a `v*` tag promote to prod. Corpus and code each pass their own gate.

**Honest cost.** Idle, the variable compute (Lambda, DynamoDB on-demand, CloudFront, Bedrock)
is **$0**. Fixed floor, a few dollars/month:
- KMS signing key: ≈$1/mo + negligible per-sign
- Route 53 hosted zone (if used): $0.50/mo
- S3 corpus storage: cents
- **AWS WAF: ≈$6–8/mo — optional.** The reserved-concurrency cap + Cognito-gated authorize +
  per-request JWT already bound abuse, so **launch without WAF** and add it only if real
  traffic shows abuse.

Realistic idle: **≈$2–4/month**, dominated by KMS + DNS (≈$1 without a custom hosted zone).
Active cost is per-request Bedrock embeds (fractions of a cent) + Lambda ms — trivial.

---

## Requirements traceability

| Requirement | How it's met |
|---|---|
| ≈$0 when idle | Lambda + DynamoDB on-demand scale to zero; ≈$2–4/mo fixed floor |
| Secure public endpoint | CloudFront+OAC → Lambda; OAuth 2.1/DCR; JWT on `/mcp`; Cognito gate on `/authorize` |
| GitHub CI/CD | OIDC keyless; PR test, main→staging, tag→prod promote-by-digest |
| Split ingestion / serving | Decoupled halves sharing only the S3 corpus artifact |
| Run locally + in CI | One FastAPI app + one Dockerfile; `PEP_ORACLE_ENV` profiles |
| Known prod version | Two version axes; `GET /version`; alias rollback |
| Keys as secure as possible | Bedrock IAM (no key); KMS-asymmetric JWT signing; SSM SecureString; no Anthropic key in scope |
| Responsive | `bge-large` off the hot path; in-memory retrieval over 10k vectors; stateless MCP |
| Multiple concurrent users | Lambda horizontal scale; per-container corpus copies; DynamoDB conditional-write rotation |

## Out of scope

- The `/ask` web UI path and its Haiku preprocessor (and `ANTHROPIC_API_KEY`).
- Replacing Modal GPU compute.
- AWS WAF at launch (optional add-on).

## Open implementation questions — resolved 2026-06-02

- **AWS region → `ap-southeast-2` (Sydney), single-region.** The user's first pick, Melbourne
  (`ap-southeast-4`), does **not** host Bedrock Titan Text Embeddings V2 — verified on the AWS
  model card (in-region support in ~21 regions including Sydney, but not Melbourne, and Titan V2
  has no Geo/Global cross-region inference profile). Cohere Embed is likewise absent from
  Melbourne's ~11-model Bedrock catalogue. Options were (a) Melbourne home + cross-region embed
  calls to Sydney, or (b) everything in Sydney; the user chose (b) for one-region simplicity.
  **Caveat that still holds:** CloudFront's ACM certificate must live in `us-east-1` regardless
  (CloudFront constraint), as must any future WAF web ACL (CLOUDFRONT scope) — so the CDK needs a
  small `us-east-1` cert/WAF stack alongside the `ap-southeast-2` stacks.
- **Custom domain → reuse `pep-oracle.iicapn.com` via Route 53.** Move DNS to a Route 53 hosted
  zone, issue the ACM cert (us-east-1, for CloudFront), point an alias at the CloudFront
  distribution. Keeps existing MCP client registrations valid.
- **Ingestion concurrency guard** — deferred to the Phase 3 (Fargate ingestion) plan; the
  serving side already tolerates concurrent reads via the atomic write-then-flip of `current.json`.

## Plan structure

Implemented as a sequence of phased plans (each independently testable), not one document:
1. Bedrock embedding backend + versioned corpus artifact + one-time backfill.
2. Serving on Lambda (OAuth→DynamoDB, JWT signing seam Phase 1, Cognito gate, CloudFront, CDK).
3. Ingestion on Fargate (EventBridge cron, Modal, publish corpus, concurrency guard).
4. CI/CD pipeline (GitHub OIDC, promote-by-digest, smoke tests).
5. KMS asymmetric JWT signing (fast-follow).
