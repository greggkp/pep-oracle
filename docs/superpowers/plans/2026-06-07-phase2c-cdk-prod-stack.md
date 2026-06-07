# Phase 2c — CDK prod serving stack (author-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Author (do **not** deploy) an AWS CDK (Python) app that provisions the single **prod** serving stack for the pep-oracle MCP endpoint — container Lambda (FastAPI+Mangum) behind CloudFront+OAC, with S3 corpus bucket, DynamoDB OAuth table, KMS, SSM HS256 signing param, a one-user Cognito pool, Route 53 + ACM, and least-privilege IAM — plus the one app-code change serving needs (`stateless_http=True`), a container `Dockerfile`, and a deploy/cutover runbook.

**Architecture:** Two CDK stacks so the CloudFront ACM cert can live in `us-east-1` while compute lives in the Bedrock region `ap-southeast-2`: a small **`PepOracleCertStack` (us-east-1)** owning the Route 53 public hosted zone for `pep-oracle.iicapn.com` + the DNS-validated ACM cert; and **`PepOracleProdStack` (ap-southeast-2)** owning everything else and referencing the cert ARN cross-region (`cross_region_references=True`). The prod stack's CloudFront distribution fronts a Lambda **Function URL** locked by **Origin Access Control** (`AWS_IAM`). The Lambda serves from the S3 corpus artifact (`SERVE_FROM_ARTIFACT=1`, Bedrock embeddings), stores OAuth state in DynamoDB, signs tokens with an HS256 key from SSM SecureString, and gates `/oauth/authorize` with the in-app Cognito gate shipped in Phase 2b2.

**Scope (decided 2026-06-07):** *Author + `cdk synth` + assertion tests only — $0 AWS spend.* No `cdk bootstrap`/`cdk deploy`, no real resources, no DNS change in this plan. The actual bootstrap, deploy, Cognito-user creation, corpus upload, NS delegation, and the **direct cutover of `pep-oracle.iicapn.com`** are documented in a runbook (Task 8) and executed only after a separate explicit go-ahead. **Single prod environment** (no staging; staging lands with the Phase 4 CI/CD pipeline). KMS *asymmetric signing* stays Phase 5 — 2c uses the HS256-from-SSM seam shipped in 2b2.

**Tech Stack:** AWS CDK v2 (`aws-cdk-lib`, Python), `aws_cdk.assertions` for template tests, Docker (AWS Lambda Python base image), pytest. The app code is FastAPI + Mangum + the `mcp` SDK (already in the repo).

**Out of scope (later phases):** ingestion on Fargate + EventBridge (Phase 3); GitHub OIDC + promote-by-digest pipeline + staging (Phase 4); KMS asymmetric JWT signing (Phase 5); AWS WAF (optional, deferred). The 3 corpus carry-forwards from the migration spec are already fixed (Phase 2a).

**Commit hook (every task):** the repo's `PreToolUse` hook blocks `git commit` unless `uv run pytest -x -q` passes **and** `.claude/.md-reviewed` exists (it is consumed each commit). Tasks that don't change `CLAUDE.md` just re-`touch .claude/.md-reviewed` before committing; Task 8 changes `CLAUDE.md`, so run `/claude-md-improver` first there. **Critical:** the root `pytest` run (what the hook executes) must NOT try to import `aws-cdk-lib` (it isn't in the project venv), so Task 2 excludes `infra/` from root pytest collection; CDK assertion tests run via the infra venv (`cd infra && python -m pytest`).

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/pep_oracle/mcp_server.py` | set `stateless_http=True` (multi-container serving) | 1 |
| `tests/test_mcp_server.py` | assert the stateless flag | 1 |
| `pyproject.toml` (root) | exclude `infra/` from root pytest collection | 2 |
| `infra/app.py` | CDK App entry: instantiate cert + prod stacks | 2,7 |
| `infra/cdk.json` | CDK app config (`python app.py`) | 2 |
| `infra/requirements.txt` | `aws-cdk-lib`, `constructs` (infra venv only) | 2 |
| `infra/pep_oracle_infra/__init__.py` | package marker | 2 |
| `infra/pep_oracle_infra/config.py` | typed deploy config (domain, region, prefixes) | 2 |
| `infra/pep_oracle_infra/prod_stack.py` | the ap-southeast-2 prod stack (data, Cognito, Lambda, CloudFront, Route53) | 4,5,6,7 |
| `infra/pep_oracle_infra/cert_stack.py` | the us-east-1 hosted zone + ACM cert | 7 |
| `infra/tests/test_prod_stack.py` | `aws_cdk.assertions` template tests | 4,5,6,7 |
| `infra/tests/test_cert_stack.py` | cert-stack template tests | 7 |
| `Dockerfile` | container image for the Lambda (`pep_oracle.server.handler`) | 3 |
| `.dockerignore` | keep the image small | 3 |
| `docs/aws/phase2c-deploy-runbook.md` | bootstrap → deploy → cutover runbook | 8 |
| `CLAUDE.md`, `.env.example` | document the infra + deploy seam | 8 |

**Deploy-config values** (used across the CDK; defined once in `infra/pep_oracle_infra/config.py`):
- domain: `pep-oracle.iicapn.com`; public URL: `https://pep-oracle.iicapn.com`
- compute region: `ap-southeast-2`; cert region: `us-east-1`
- corpus bucket name: `pep-oracle-corpus-prod` (globally-unique; override via context)
- DynamoDB table: `pep-oracle-oauth` (matches `config.OAUTH_DDB_TABLE` default)
- SSM signing param: `/pep-oracle/oauth-signing-key` (matches `config.OAUTH_SIGNING_SSM_PARAM` default)
- Cognito Hosted-UI domain prefix: `pep-oracle-prod` (globally-unique; override via context)
- embed model: `amazon.titan-embed-text-v2:0`; allowed login email: supplied via context `allowed_email`

---

## Task 1: Serving must run stateless (`stateless_http=True`)

The migration spec requires `stateless_http=True` so any Lambda container can serve any MCP request (no sticky sessions). It is currently unset. This is the one runtime-behavior change in Phase 2c; everything else is infra. Set it where the MCP server is configured, next to the existing `streamable_http_path`/transport-security mount logic.

**Files:**
- Modify: `src/pep_oracle/mcp_server.py` (set the flag on the `mcp` instance)
- Test: `tests/test_mcp_server.py`

- [ ] **Step 1: Find where `mcp` is instantiated.** Read `src/pep_oracle/mcp_server.py` and locate the `FastMCP(...)` construction (the module-level `mcp = FastMCP(...)`). Note whether `stateless_http` is passed at construction or set via `mcp.settings`. (FastMCP accepts `stateless_http=True` as a constructor kwarg and also exposes `mcp.settings.stateless_http`.)

- [ ] **Step 2: Write the failing test**

Append to `tests/test_mcp_server.py` (create the file with the standard import header if it does not exist):

```python
def test_mcp_is_stateless_http():
    """Multi-container Lambda serving requires stateless MCP (no per-session state)."""
    from pep_oracle.mcp_server import mcp

    assert mcp.settings.stateless_http is True
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py::test_mcp_is_stateless_http -q`
Expected: FAIL — `assert False is True` (flag defaults to False).

- [ ] **Step 4: Set the flag**

In `src/pep_oracle/mcp_server.py`, pass `stateless_http=True` to the `FastMCP(...)` constructor. If the constructor call is e.g. `mcp = FastMCP("pep-oracle", ...)`, add the kwarg: `mcp = FastMCP("pep-oracle", ..., stateless_http=True)`. (If the SDK version rejects the kwarg, instead set `mcp.settings.stateless_http = True` on the line immediately after construction.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -q`
Expected: PASS.

- [ ] **Step 6: Run the full suite (no regression)**

Run: `uv run pytest -q`
Expected: PASS — in particular the existing MCP mount tests in `tests/test_server.py` still pass (the mount remaps `streamable_http_path` and now also serves stateless).

- [ ] **Step 7: Commit**

```bash
touch .claude/.md-reviewed
git add src/pep_oracle/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): stateless_http=True for multi-container Lambda serving"
```

---

## Task 2: CDK project scaffold (isolated from the root venv)

Create a self-contained CDK app under `infra/` with its own requirements, an App entry that wires two (initially near-empty) stacks, a typed deploy-config, and a smoke synth test. Crucially, exclude `infra/` from the **root** pytest run so the commit hook (`uv run pytest -x -q`) never imports `aws-cdk-lib`.

**Files:**
- Modify: `pyproject.toml` (root) — exclude `infra` from pytest collection
- Create: `infra/cdk.json`, `infra/requirements.txt`, `infra/app.py`, `infra/pep_oracle_infra/__init__.py`, `infra/pep_oracle_infra/config.py`, `infra/pep_oracle_infra/prod_stack.py`, `infra/pep_oracle_infra/cert_stack.py`, `infra/tests/__init__.py`, `infra/tests/test_synth.py`

- [ ] **Step 1: Exclude `infra/` from the root pytest run**

Read `pyproject.toml`. If a `[tool.pytest.ini_options]` table exists, add `infra` to its ignore list; otherwise add the table. The robust form (works regardless of existing config) — ensure this key is present:

```toml
[tool.pytest.ini_options]
addopts = "--ignore=infra"
```

If `[tool.pytest.ini_options]` already exists with an `addopts`, append ` --ignore=infra` to the existing string instead of duplicating the key. Then verify the root run is unaffected:

Run: `uv run pytest -q`
Expected: PASS, and the collected count is unchanged from before this task (no `infra` tests collected).

- [ ] **Step 2: Create the infra Python venv + deps**

Create `infra/requirements.txt`:

```
aws-cdk-lib==2.150.0
constructs>=10.0.0,<11.0.0
pytest>=8.0
```

Create the infra venv and install (kept separate from the project `uv` venv):

```bash
cd /opt/pep-oracle/app/infra
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

(If `aws-cdk-lib==2.150.0` is unavailable, pin the latest `2.x` the index offers and note it; the constructs used here — `FunctionUrlOrigin.with_origin_access_control`, `cross_region_references` — require `aws-cdk-lib>=2.130`.)

- [ ] **Step 3: Create `infra/cdk.json`**

```json
{
  "app": "python app.py",
  "context": {
    "@aws-cdk/core:newStyleStackSynthesis": true,
    "domain_name": "pep-oracle.iicapn.com",
    "compute_region": "ap-southeast-2",
    "cert_region": "us-east-1",
    "corpus_bucket_name": "pep-oracle-corpus-prod",
    "cognito_domain_prefix": "pep-oracle-prod",
    "allowed_email": "REPLACE_ME@example.com"
  }
}
```

- [ ] **Step 4: Create `infra/pep_oracle_infra/__init__.py`** (empty file).

- [ ] **Step 5: Create `infra/pep_oracle_infra/config.py`**

```python
"""Typed deploy-time config for the pep-oracle CDK app.

Values come from cdk.json context (overridable with -c key=value). One source of
truth shared by app.py and the stacks. Mirrors the runtime env-var contract in
src/pep_oracle/config.py — the Lambda env is set from these in prod_stack.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeployConfig:
    domain_name: str
    compute_region: str
    cert_region: str
    corpus_bucket_name: str
    cognito_domain_prefix: str
    allowed_email: str
    # runtime contract (matches src/pep_oracle/config.py defaults)
    embed_model: str = "amazon.titan-embed-text-v2:0"
    embed_dims: str = "1024"
    oauth_table_name: str = "pep-oracle-oauth"
    signing_ssm_param: str = "/pep-oracle/oauth-signing-key"

    @property
    def public_url(self) -> str:
        return f"https://{self.domain_name}"

    @classmethod
    def from_node(cls, node) -> "DeployConfig":
        def ctx(key: str, default=None):
            val = node.try_get_context(key)
            return val if val is not None else default

        return cls(
            domain_name=ctx("domain_name", "pep-oracle.iicapn.com"),
            compute_region=ctx("compute_region", "ap-southeast-2"),
            cert_region=ctx("cert_region", "us-east-1"),
            corpus_bucket_name=ctx("corpus_bucket_name", "pep-oracle-corpus-prod"),
            cognito_domain_prefix=ctx("cognito_domain_prefix", "pep-oracle-prod"),
            allowed_email=ctx("allowed_email", "REPLACE_ME@example.com"),
        )
```

- [ ] **Step 6: Create placeholder stack modules**

`infra/pep_oracle_infra/cert_stack.py`:

```python
"""us-east-1 stack: Route 53 hosted zone for the MCP domain + the CloudFront ACM cert.

CloudFront requires its ACM cert in us-east-1, so the zone+cert live here and the
prod stack (ap-southeast-2) references the cert ARN cross-region. Resources are
added in Task 7.
"""

from __future__ import annotations

from aws_cdk import Stack
from constructs import Construct

from pep_oracle_infra.config import DeployConfig


class PepOracleCertStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: DeployConfig, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)
        self.cfg = cfg
        # Task 7 adds: PublicHostedZone + Certificate (DNS-validated).
```

`infra/pep_oracle_infra/prod_stack.py`:

```python
"""ap-southeast-2 prod stack: data layer, Cognito, Lambda + Function URL, CloudFront,
Route 53 alias. Resources are added in Tasks 4-7.
"""

from __future__ import annotations

from typing import Optional

from aws_cdk import Stack
from constructs import Construct

from pep_oracle_infra.config import DeployConfig


class PepOracleProdStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        cfg: DeployConfig,
        cert_arn: Optional[str] = None,
        hosted_zone_id: Optional[str] = None,
        hosted_zone_name: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, cid, **kwargs)
        self.cfg = cfg
        self._cert_arn = cert_arn
        self._hosted_zone_id = hosted_zone_id
        self._hosted_zone_name = hosted_zone_name
        # Task 4: KMS + S3 corpus bucket + DynamoDB OAuth table
        # Task 5: Cognito user pool + domain + app client
        # Task 6: Lambda (container) + Function URL + IAM
        # Task 7: CloudFront (cert cross-region) + Route 53 alias
```

- [ ] **Step 7: Create `infra/app.py`**

```python
#!/usr/bin/env python3
"""CDK app entry for the pep-oracle prod serving stack (Phase 2c).

Two stacks: the us-east-1 cert/zone stack and the ap-southeast-2 prod stack, wired
with cross_region_references so the prod CloudFront can use the us-east-1 cert.
"""

import os

import aws_cdk as cdk

from pep_oracle_infra.cert_stack import PepOracleCertStack
from pep_oracle_infra.config import DeployConfig
from pep_oracle_infra.prod_stack import PepOracleProdStack

app = cdk.App()
cfg = DeployConfig.from_node(app.node)

account = os.environ.get("CDK_DEFAULT_ACCOUNT")

cert_stack = PepOracleCertStack(
    app,
    "PepOracleCertStack",
    cfg=cfg,
    cross_region_references=True,
    env=cdk.Environment(account=account, region=cfg.cert_region),
)

PepOracleProdStack(
    app,
    "PepOracleProdStack",
    cfg=cfg,
    cross_region_references=True,
    env=cdk.Environment(account=account, region=cfg.compute_region),
)

app.synth()
```

(Task 7 rewires `app.py` to pass the cert/zone references from `cert_stack` into the prod stack. For now the prod stack stands alone so each task synthesizes.)

- [ ] **Step 8: Create `infra/tests/__init__.py`** (empty) **and `infra/tests/test_synth.py`**

```python
"""Synth smoke test: both stacks synthesize to a CloudFormation template."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Template

from pep_oracle_infra.config import DeployConfig
from pep_oracle_infra.cert_stack import PepOracleCertStack
from pep_oracle_infra.prod_stack import PepOracleProdStack

ENV = cdk.Environment(account="111111111111", region="ap-southeast-2")
CERT_ENV = cdk.Environment(account="111111111111", region="us-east-1")


def _cfg() -> DeployConfig:
    return DeployConfig(
        domain_name="pep-oracle.iicapn.com",
        compute_region="ap-southeast-2",
        cert_region="us-east-1",
        corpus_bucket_name="pep-oracle-corpus-test",
        cognito_domain_prefix="pep-oracle-test",
        allowed_email="me@example.com",
    )


def test_prod_stack_synthesizes():
    app = cdk.App()
    stack = PepOracleProdStack(app, "Prod", cfg=_cfg(), cross_region_references=True, env=ENV)
    Template.from_stack(stack)  # raises if synthesis fails


def test_cert_stack_synthesizes():
    app = cdk.App()
    stack = PepOracleCertStack(app, "Cert", cfg=_cfg(), cross_region_references=True, env=CERT_ENV)
    Template.from_stack(stack)
```

- [ ] **Step 9: Run the infra tests + a real synth**

```bash
cd /opt/pep-oracle/app/infra
.venv/bin/python -m pytest tests/ -q
.venv/bin/cdk synth -c allowed_email=me@example.com >/dev/null && echo "synth OK"   # if the cdk CLI is installed
```

Expected: pytest PASS (2 passed). The `cdk synth` line is best-effort (needs the `cdk` CLI / Node); the assertion tests are the authoritative gate and use only `aws-cdk-lib`.

- [ ] **Step 10: Commit**

```bash
touch .claude/.md-reviewed
git add pyproject.toml infra/
git commit -m "feat(infra): CDK app scaffold (cert + prod stacks, deploy config, synth test)"
```

(Do not commit `infra/.venv/` — add `infra/.venv/` to `.gitignore` in this step if not already covered by an existing `.venv` ignore.)

---

## Task 3: Container `Dockerfile` for the serving Lambda

A container-image Lambda (not a zip) is the migration's single runtime artifact. Build on the AWS Lambda Python base image, install the package with its `server` + `aws` extras, and point the image's command at `pep_oracle.server.handler` (the Mangum adapter confirmed at `server.py`).

**Files:**
- Create: `Dockerfile`, `.dockerignore`

- [ ] **Step 1: Create `Dockerfile`**

```dockerfile
# Serving Lambda image — FastAPI + Mangum, served via the corpus artifact (no ChromaDB).
# Base: AWS Lambda Python runtime interface (includes the RIC). Region-agnostic.
FROM public.ecr.aws/lambda/python:3.12

# Build deps for any wheels without manylinux (kept minimal; pyarrow/boto3 ship wheels).
COPY pyproject.toml ${LAMBDA_TASK_ROOT}/
COPY src/ ${LAMBDA_TASK_ROOT}/src/

# Install the package + the server and aws extras (fastapi, mcp, pyjwt, mangum, boto3, pyarrow).
# fastembed/chromadb come via base deps but are NOT used on the artifact serve path; they
# stay importable. --no-cache-dir keeps the image lean.
RUN python -m pip install --no-cache-dir "${LAMBDA_TASK_ROOT}[server,aws]"

# Mangum adapter exported at module import.
CMD ["pep_oracle.server.handler"]
```

- [ ] **Step 2: Create `.dockerignore`**

```
.git
.venv
infra/.venv
infra/cdk.out
tests
docs
.pep-oracle
**/__pycache__
*.pyc
.playwright-mcp
.superpowers
```

- [ ] **Step 3: Verify the image builds (local, no AWS spend)**

If Docker is available:

```bash
cd /opt/pep-oracle/app
docker build -t pep-oracle-serve:plan-check .
docker run --rm --entrypoint python pep-oracle-serve:plan-check -c "import pep_oracle.server as s; assert s.handler is not None; print('handler OK')"
```

Expected: image builds; `handler OK` printed (Mangum wrapped the app; `server.handler` is not None because `mangum` is installed via the `aws` extra).

If Docker is **not** available in this environment, record that the build step is deferred to the deploy runbook (Task 8) and verify structurally instead: confirm `pyproject.toml` exposes `[server,aws]` extras containing `mangum`, and that `pep_oracle.server.handler` exists (already confirmed). Report this clearly rather than claiming the build passed.

- [ ] **Step 4: Commit**

```bash
touch .claude/.md-reviewed
git add Dockerfile .dockerignore
git commit -m "feat(infra): container Dockerfile for the serving Lambda (Mangum handler)"
```

---

## Task 4: Data layer — KMS key + S3 corpus bucket + DynamoDB OAuth table

Add the encrypted, versioned corpus bucket and the OAuth table to the prod stack. **The table schema must match `DynamoDbStore` exactly** (verified): partition key `pk` (S), GSI `family-index` keyed on `family_id` (S) with `KEYS_ONLY` projection, native TTL on attribute `ttl`, on-demand billing.

**Files:**
- Modify: `infra/pep_oracle_infra/prod_stack.py`
- Test: `infra/tests/test_prod_stack.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `infra/tests/test_prod_stack.py`:

```python
"""Template assertions for PepOracleProdStack."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from pep_oracle_infra.config import DeployConfig
from pep_oracle_infra.prod_stack import PepOracleProdStack

ENV = cdk.Environment(account="111111111111", region="ap-southeast-2")


def _cfg() -> DeployConfig:
    return DeployConfig(
        domain_name="pep-oracle.iicapn.com",
        compute_region="ap-southeast-2",
        cert_region="us-east-1",
        corpus_bucket_name="pep-oracle-corpus-test",
        cognito_domain_prefix="pep-oracle-test",
        allowed_email="me@example.com",
    )


def _template() -> Template:
    app = cdk.App()
    stack = PepOracleProdStack(
        app, "Prod", cfg=_cfg(),
        cert_arn="arn:aws:acm:us-east-1:111111111111:certificate/abc",
        hosted_zone_id="Z123456ABCDEFG",
        hosted_zone_name="pep-oracle.iicapn.com",
        cross_region_references=True, env=ENV,
    )
    return Template.from_stack(stack)


def test_dynamodb_table_matches_store_schema():
    t = _template()
    t.has_resource_properties("AWS::DynamoDB::Table", Match.object_like({
        "TableName": "pep-oracle-oauth",
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
        "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
        "GlobalSecondaryIndexes": Match.array_with([
            Match.object_like({
                "IndexName": "family-index",
                "KeySchema": [{"AttributeName": "family_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            })
        ]),
    }))


def test_corpus_bucket_is_private_versioned_encrypted():
    t = _template()
    t.has_resource_properties("AWS::S3::Bucket", Match.object_like({
        "VersioningConfiguration": {"Status": "Enabled"},
        "PublicAccessBlockConfiguration": Match.object_like({
            "BlockPublicAcls": True, "RestrictPublicBuckets": True,
        }),
    }))


def test_kms_key_present():
    t = _template()
    t.resource_count_is("AWS::KMS::Key", 1)
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /opt/pep-oracle/app/infra
.venv/bin/python -m pytest tests/test_prod_stack.py -q
```

Expected: FAIL (no DynamoDB/S3/KMS resources yet).

- [ ] **Step 3: Implement the data layer**

In `infra/pep_oracle_infra/prod_stack.py`, add imports at the top:

```python
from aws_cdk import RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
```

Then at the end of `__init__` (replacing the `# Task 4` comment), add:

```python
        self.kms_key = kms.Key(
            self, "DataKey",
            description="pep-oracle encryption-at-rest (S3 corpus, DynamoDB, SSM signing key)",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.corpus_bucket = s3.Bucket(
            self, "CorpusBucket",
            bucket_name=cfg.corpus_bucket_name,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.kms_key,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.oauth_table = dynamodb.Table(
            self, "OAuthTable",
            table_name=cfg.oauth_table_name,
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            time_to_live_attribute="ttl",
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.kms_key,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.oauth_table.add_global_secondary_index(
            index_name="family-index",
            partition_key=dynamodb.Attribute(
                name="family_id", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /opt/pep-oracle/app/infra
.venv/bin/python -m pytest tests/test_prod_stack.py -q
```

Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
cd /opt/pep-oracle/app
touch .claude/.md-reviewed
git add infra/pep_oracle_infra/prod_stack.py infra/tests/test_prod_stack.py
git commit -m "feat(infra): KMS key + versioned S3 corpus bucket + DynamoDB OAuth table"
```

---

## Task 5: Cognito — one-user pool + Hosted-UI domain + confidential app client

Add the Cognito identity provider the in-app gate (Phase 2b2) brokers against: a user pool (email sign-in), a Hosted-UI `cognito_domain`, and a **confidential** app client (generates a secret; authorization-code grant; scopes `openid email`; callback = `https://pep-oracle.iicapn.com/oauth/authorize/callback` — the `CALLBACK_PATH` from `authorize_gate`).

**Files:**
- Modify: `infra/pep_oracle_infra/prod_stack.py`
- Test: `infra/tests/test_prod_stack.py` (append)

- [ ] **Step 1: Write the failing tests** — append to `infra/tests/test_prod_stack.py`:

```python
def test_cognito_user_pool_and_domain():
    t = _template()
    t.resource_count_is("AWS::Cognito::UserPool", 1)
    t.has_resource_properties("AWS::Cognito::UserPoolDomain", Match.object_like({
        "Domain": "pep-oracle-test",
    }))


def test_cognito_client_is_confidential_auth_code():
    t = _template()
    t.has_resource_properties("AWS::Cognito::UserPoolClient", Match.object_like({
        "GenerateSecret": True,
        "AllowedOAuthFlows": ["code"],
        "AllowedOAuthScopes": Match.array_with(["openid", "email"]),
        "CallbackURLs": ["https://pep-oracle.iicapn.com/oauth/authorize/callback"],
        "SupportedIdentityProviders": ["COGNITO"],
    }))
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /opt/pep-oracle/app/infra && .venv/bin/python -m pytest tests/test_prod_stack.py -k cognito -q
```

Expected: FAIL (no Cognito resources yet).

- [ ] **Step 3: Implement Cognito**

Add imports to `prod_stack.py`:

```python
from aws_cdk import aws_cognito as cognito
```

Add to `__init__` (after the data layer):

```python
        self.user_pool = cognito.UserPool(
            self, "UserPool",
            sign_in_aliases=cognito.SignInAliases(email=True),
            self_sign_up_enabled=False,  # single operator-created user
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.user_pool_domain = self.user_pool.add_domain(
            "HostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=cfg.cognito_domain_prefix
            ),
        )
        self.user_pool_client = self.user_pool.add_client(
            "AppClient",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"{cfg.public_url}/oauth/authorize/callback"],
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
            prevent_user_existence_errors=True,
        )
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /opt/pep-oracle/app/infra && .venv/bin/python -m pytest tests/test_prod_stack.py -q
```

Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
cd /opt/pep-oracle/app
touch .claude/.md-reviewed
git add infra/pep_oracle_infra/prod_stack.py infra/tests/test_prod_stack.py
git commit -m "feat(infra): Cognito user pool + Hosted-UI domain + confidential app client"
```

---

## Task 6: Serving Lambda (container) + Function URL + least-privilege IAM

Add the container Lambda from the `Dockerfile` (Task 3), wire its full env (the runtime contract from `config.py`, with the Cognito + corpus + DynamoDB + SSM + Bedrock values), put it behind a Function URL with `AWS_IAM` auth (CloudFront-only via OAC in Task 7), set a reserved-concurrency cap, and grant least-privilege IAM. The SSM SecureString signing param is created **out-of-band** (runbook); the Lambda is granted `ssm:GetParameter` on its ARN + `kms:Decrypt` on the data key.

**Files:**
- Modify: `infra/pep_oracle_infra/prod_stack.py`
- Test: `infra/tests/test_prod_stack.py` (append)

- [ ] **Step 1: Write the failing tests** — append:

```python
def test_lambda_env_has_serving_contract():
    t = _template()
    t.has_resource_properties("AWS::Lambda::Function", Match.object_like({
        "PackageType": "Image",
        "ReservedConcurrentExecutions": 30,
        "Environment": {"Variables": Match.object_like({
            "PEP_ORACLE_SERVE_FROM_ARTIFACT": "1",
            "PEP_ORACLE_EMBED_BACKEND": "bedrock",
            "PEP_ORACLE_EMBED_MODEL": "amazon.titan-embed-text-v2:0",
            "PEP_ORACLE_OAUTH_STORE": "dynamodb",
            "PEP_ORACLE_OAUTH_DDB_TABLE": "pep-oracle-oauth",
            "PEP_ORACLE_OAUTH_SIGNING_BACKEND": "ssm",
            "PEP_ORACLE_OAUTH_SIGNING_SSM_PARAM": "/pep-oracle/oauth-signing-key",
            "PEP_ORACLE_AUTHORIZE_GATE": "cognito",
            "PEP_ORACLE_PUBLIC_URL": "https://pep-oracle.iicapn.com",
            "PEP_ORACLE_CORPUS_URI": "s3://pep-oracle-corpus-test",
        })},
    }))


def test_function_url_is_iam_auth():
    t = _template()
    t.has_resource_properties("AWS::Lambda::Url", Match.object_like({
        "AuthType": "AWS_IAM",
    }))


def test_lambda_role_has_bedrock_and_ssm():
    t = _template()
    # Bedrock InvokeModel on the embed model + SSM GetParameter on the signing param
    t.has_resource_properties("AWS::IAM::Policy", Match.object_like({
        "PolicyDocument": Match.object_like({
            "Statement": Match.array_with([
                Match.object_like({"Action": "bedrock:InvokeModel"}),
                Match.object_like({"Action": "ssm:GetParameter"}),
            ])
        })
    }))
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /opt/pep-oracle/app/infra && .venv/bin/python -m pytest tests/test_prod_stack.py -k "lambda or function_url" -q
```

Expected: FAIL (no Lambda/Url yet).

- [ ] **Step 3: Implement the Lambda + Function URL + IAM**

Add imports to `prod_stack.py`:

```python
from pathlib import Path

from aws_cdk import Duration
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
```

Add to `__init__` (after Cognito). Note `_PROJECT_ROOT` resolves the repo root (two levels up from this file: `infra/pep_oracle_infra/prod_stack.py` → repo root):

```python
        project_root = Path(__file__).resolve().parents[2]

        env = {
            "PEP_ORACLE_SERVE_FROM_ARTIFACT": "1",
            "PEP_ORACLE_EMBED_BACKEND": "bedrock",
            "PEP_ORACLE_BEDROCK_REGION": cfg.compute_region,
            "PEP_ORACLE_EMBED_MODEL": cfg.embed_model,
            "PEP_ORACLE_EMBED_DIMS": cfg.embed_dims,
            "PEP_ORACLE_CORPUS_URI": f"s3://{cfg.corpus_bucket_name}",
            "PEP_ORACLE_OAUTH_STORE": "dynamodb",
            "PEP_ORACLE_OAUTH_DDB_TABLE": cfg.oauth_table_name,
            "PEP_ORACLE_OAUTH_DDB_REGION": cfg.compute_region,
            "PEP_ORACLE_OAUTH_SIGNING_BACKEND": "ssm",
            "PEP_ORACLE_OAUTH_SIGNING_SSM_PARAM": cfg.signing_ssm_param,
            "PEP_ORACLE_OAUTH_SIGNING_SSM_REGION": cfg.compute_region,
            "PEP_ORACLE_AUTHORIZE_GATE": "cognito",
            "PEP_ORACLE_COGNITO_DOMAIN": (
                f"https://{cfg.cognito_domain_prefix}.auth.{cfg.compute_region}.amazoncognito.com"
            ),
            "PEP_ORACLE_COGNITO_CLIENT_ID": self.user_pool_client.user_pool_client_id,
            "PEP_ORACLE_COGNITO_CLIENT_SECRET": (
                self.user_pool_client.user_pool_client_secret.unsafe_unwrap()
            ),
            "PEP_ORACLE_COGNITO_USER_POOL_ID": self.user_pool.user_pool_id,
            "PEP_ORACLE_COGNITO_REGION": cfg.compute_region,
            "PEP_ORACLE_COGNITO_ALLOWED_EMAILS": cfg.allowed_email,
            "PEP_ORACLE_PUBLIC_URL": cfg.public_url,
        }

        self.fn = lambda_.DockerImageFunction(
            self, "ServeFn",
            code=lambda_.DockerImageCode.from_image_asset(str(project_root)),
            memory_size=2048,
            timeout=Duration.seconds(30),
            reserved_concurrent_executions=30,
            environment=env,
        )

        # Least-privilege grants
        self.corpus_bucket.grant_read(self.fn)
        self.oauth_table.grant_read_write_data(self.fn)
        self.kms_key.grant_decrypt(self.fn)  # SSM SecureString + S3/DDB CMK reads
        self.fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{cfg.compute_region}::foundation-model/{cfg.embed_model}"
            ],
        ))
        self.fn.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[
                f"arn:aws:ssm:{cfg.compute_region}:{self.account}:parameter{cfg.signing_ssm_param}"
            ],
        ))

        self.fn_url = self.fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM
        )
```

Note on the secret: `user_pool_client_secret.unsafe_unwrap()` resolves the Cognito app-client secret into the Lambda env at deploy time. For a single-user box this is acceptable (the env is encrypted at rest by Lambda and visible only with IAM read on the function/CFN). A future hardening (note in Task 8 docs) is to move it to SSM SecureString and have the app read it. `user_pool_client_secret` requires `aws-cdk-lib>=2.130`; if unavailable, fall back to the documented Cognito `describe-user-pool-client` custom-resource pattern and STOP to report.

- [ ] **Step 4: Run to verify they pass**

```bash
cd /opt/pep-oracle/app/infra && .venv/bin/python -m pytest tests/test_prod_stack.py -q
```

Expected: PASS (8 passed). Note: `DockerImageCode.from_image_asset` does not build the image during `Template.from_stack` synthesis in tests (it stages an asset); the assertion tests pass without Docker. A real `cdk deploy` builds it (Task 8 / Docker required then).

- [ ] **Step 5: Commit**

```bash
cd /opt/pep-oracle/app
touch .claude/.md-reviewed
git add infra/pep_oracle_infra/prod_stack.py infra/tests/test_prod_stack.py
git commit -m "feat(infra): container serving Lambda + Function URL (IAM) + least-privilege IAM"
```

---

## Task 7: CloudFront + ACM cert (us-east-1) + Route 53 — the public endpoint

Front the Function URL with CloudFront locked by OAC; attach the custom domain `pep-oracle.iicapn.com` with the us-east-1 ACM cert; create the Route 53 hosted zone (cert stack) and the A/AAAA alias (prod stack). The cert stack owns the **zone + cert** (so cert DNS-validation is in-stack, no cycle); the prod stack references the cert ARN + zone cross-region.

**Files:**
- Modify: `infra/pep_oracle_infra/cert_stack.py`, `infra/pep_oracle_infra/prod_stack.py`, `infra/app.py`
- Test: `infra/tests/test_cert_stack.py` (new), `infra/tests/test_prod_stack.py` (append)

- [ ] **Step 1: Write the failing tests**

Create `infra/tests/test_cert_stack.py`:

```python
"""Template assertions for PepOracleCertStack (us-east-1)."""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from pep_oracle_infra.cert_stack import PepOracleCertStack
from pep_oracle_infra.config import DeployConfig

CERT_ENV = cdk.Environment(account="111111111111", region="us-east-1")


def _cfg() -> DeployConfig:
    return DeployConfig(
        domain_name="pep-oracle.iicapn.com", compute_region="ap-southeast-2",
        cert_region="us-east-1", corpus_bucket_name="b", cognito_domain_prefix="p",
        allowed_email="me@example.com",
    )


def _t() -> Template:
    app = cdk.App()
    s = PepOracleCertStack(app, "Cert", cfg=_cfg(), cross_region_references=True, env=CERT_ENV)
    return Template.from_stack(s)


def test_hosted_zone_for_domain():
    _t().has_resource_properties("AWS::Route53::HostedZone", Match.object_like({
        "Name": "pep-oracle.iicapn.com.",
    }))


def test_certificate_for_domain():
    _t().has_resource_properties("AWS::CertificateManager::Certificate", Match.object_like({
        "DomainName": "pep-oracle.iicapn.com",
    }))
```

Append to `infra/tests/test_prod_stack.py`:

```python
def test_cloudfront_distribution_has_domain_and_oac_origin():
    t = _template()
    t.has_resource_properties("AWS::CloudFront::Distribution", Match.object_like({
        "DistributionConfig": Match.object_like({
            "Aliases": ["pep-oracle.iicapn.com"],
        })
    }))
    # OAC is created for the Function URL origin
    t.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)


def test_route53_alias_record_present():
    t = _template()
    t.has_resource_properties("AWS::Route53::RecordSet", Match.object_like({
        "Type": "A",
        "Name": "pep-oracle.iicapn.com.",
    }))
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /opt/pep-oracle/app/infra && .venv/bin/python -m pytest tests/ -q
```

Expected: FAIL — cert-stack tests (no zone/cert) and the two new prod-stack tests (no distribution/record).

- [ ] **Step 3: Implement the cert stack**

Replace the body of `infra/pep_oracle_infra/cert_stack.py` `__init__` (after `self.cfg = cfg`) with:

```python
        from aws_cdk import aws_certificatemanager as acm
        from aws_cdk import aws_route53 as route53

        self.hosted_zone = route53.PublicHostedZone(
            self, "Zone", zone_name=cfg.domain_name
        )
        self.certificate = acm.Certificate(
            self, "Cert",
            domain_name=cfg.domain_name,
            validation=acm.CertificateValidation.from_dns(self.hosted_zone),
        )
```

- [ ] **Step 4: Implement CloudFront + alias in the prod stack**

Add imports to `prod_stack.py`:

```python
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
```

Add to `__init__` (after the Lambda/Function URL). This runs only when the cross-region references were supplied (always true via `app.py`; the data/cognito/lambda tests pass dummy values through `_template()`):

```python
        cert = acm.Certificate.from_certificate_arn(self, "Cert", self._cert_arn)
        zone = route53.PublicHostedZone.from_public_hosted_zone_attributes(
            self, "Zone",
            hosted_zone_id=self._hosted_zone_id,
            zone_name=self._hosted_zone_name,
        )

        self.distribution = cloudfront.Distribution(
            self, "Cdn",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.FunctionUrlOrigin.with_origin_access_control(self.fn_url),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            ),
            domain_names=[cfg.domain_name],
            certificate=cert,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )

        route53.ARecord(
            self, "AliasA",
            zone=zone,
            record_name=cfg.domain_name,
            target=route53.RecordTarget.from_alias(
                route53_targets.CloudFrontTarget(self.distribution)
            ),
        )
```

Notes: `FunctionUrlOrigin.with_origin_access_control` auto-creates the OAC **and** the Lambda resource-policy permission for CloudFront — no manual `add_permission` needed. `ALL_VIEWER_EXCEPT_HOST_HEADER` forwards the `Authorization` header (needed for `/mcp` bearer + `/oauth/token`) while letting the Function URL set its own Host. `CACHING_DISABLED` because every route is dynamic/authenticated.

**Cross-region reference (highest-risk bit — verify at synth):** the prod stack consumes the us-east-1 cert via a plain ARN string (`cert_stack.certificate.certificate_arn` token, passed through `app.py` in Step 5) + `acm.Certificate.from_certificate_arn`, with `cross_region_references=True` on both stacks. When you run `cd infra && .venv/bin/cdk synth PepOracleProdStack` (needs the cdk CLI), confirm CDK emits the cross-region SSM export/lookup wiring. If a raw ARN-token reference does NOT trigger it, fall back to the construct-passing form the CDK docs show: change the prod-stack signature to accept the `acm.ICertificate` and `route53.IHostedZone` constructs directly and pass `cert_stack.certificate` / `cert_stack.hosted_zone` from `app.py` (update the test helper to build a tiny cert stack, or `Certificate.from_certificate_arn`/`HostedZone.from_...` with dummies). The assertion tests in this task don't exercise cross-region (they pass dummy strings), so this verification is a `cdk synth` step, not a unit test.

- [ ] **Step 5: Rewire `app.py` to pass cert + zone into the prod stack**

Replace the `PepOracleProdStack(...)` call in `infra/app.py` with:

```python
prod = PepOracleProdStack(
    app,
    "PepOracleProdStack",
    cfg=cfg,
    cert_arn=cert_stack.certificate.certificate_arn,
    hosted_zone_id=cert_stack.hosted_zone.hosted_zone_id,
    hosted_zone_name=cert_stack.hosted_zone.zone_name,
    cross_region_references=True,
    env=cdk.Environment(account=account, region=cfg.compute_region),
)
prod.add_dependency(cert_stack)
```

- [ ] **Step 6: Run all infra tests + synth**

```bash
cd /opt/pep-oracle/app/infra && .venv/bin/python -m pytest tests/ -q
```

Expected: PASS (cert: 2; prod: 10). The prod-stack `_template()` helper supplies dummy `cert_arn`/`hosted_zone_*`, so CloudFront + the alias synthesize in isolation.

- [ ] **Step 7: Commit**

```bash
cd /opt/pep-oracle/app
touch .claude/.md-reviewed
git add infra/pep_oracle_infra/cert_stack.py infra/pep_oracle_infra/prod_stack.py infra/app.py infra/tests/
git commit -m "feat(infra): CloudFront+OAC over Function URL, us-east-1 ACM cert, Route53 alias"
```

---

## Task 8: Deploy/cutover runbook + docs

Author the operator runbook for the (separately-authorized) deploy and the **direct cutover** of `pep-oracle.iicapn.com`, plus update `CLAUDE.md` and `.env.example`. No AWS actions here — documentation only.

**Files:**
- Create: `docs/aws/phase2c-deploy-runbook.md`
- Modify: `CLAUDE.md`, `.env.example`

- [ ] **Step 1: Write `docs/aws/phase2c-deploy-runbook.md`**

```markdown
# Phase 2c — Deploy & cutover runbook (pep-oracle prod serving stack)

Authored by the Phase 2c plan; **execute only after explicit go-ahead**. Provisions
real, billable resources (≈$2-4/mo idle) and performs a live DNS cutover of
pep-oracle.iicapn.com. Region: ap-southeast-2 (compute) + us-east-1 (CloudFront cert).
AWS profile: the OptiPlex default (e.g. `optiplex-cli`, account 940831808393).

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

## 3. Create the SSM SecureString signing key (encrypted with the stack KMS key)
The CDK grants the Lambda `ssm:GetParameter` + `kms:Decrypt`; create the value out-of-band
AFTER the prod stack exists (so the KMS key id is known) — or create it with the AWS-managed
SSM key now and re-key later. Simplest ordering: deploy prod (step 4) → read the DataKey id
from stack outputs → then:
```bash
KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
aws ssm put-parameter --name /pep-oracle/oauth-signing-key --type SecureString \
  --value "$KEY" --key-id <DataKey-id-from-outputs> --region ap-southeast-2
```

## 4. Deploy the prod stack (builds + pushes the Lambda image)
```bash
.venv/bin/cdk deploy PepOracleProdStack -c allowed_email=<you@example.com>
```

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
The A-alias in the prod stack already points the apex of the delegated zone at CloudFront,
so once the NS delegation (step 2) is live, `https://pep-oracle.iicapn.com` resolves to the
new stack. **Cutover = the moment delegation propagates.** To switch from the existing
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
```

- [ ] **Step 2: Update `.env.example`** — append a pointer (no new app vars; the Lambda env is set by CDK):

```bash

# --- AWS prod serving (Phase 2c) ---
# The serving Lambda's env is set by the CDK (infra/), not this file. See
# docs/aws/phase2c-deploy-runbook.md. Prod sets: EMBED_BACKEND=bedrock,
# SERVE_FROM_ARTIFACT=1, OAUTH_STORE=dynamodb, OAUTH_SIGNING_BACKEND=ssm,
# AUTHORIZE_GATE=cognito, CORPUS_URI=s3://pep-oracle-corpus-prod, plus COGNITO_*.
```

- [ ] **Step 3: Update `CLAUDE.md`**

Add a deployment bullet under `## Deployment` (after the systemd units list):

```markdown
- **AWS prod serving (Phase 2c, `infra/`)**: CDK Python app — `PepOracleCertStack` (us-east-1: Route 53 zone + ACM cert) + `PepOracleProdStack` (ap-southeast-2: KMS, S3 corpus bucket, DynamoDB OAuth table, Cognito one-user pool, container Lambda `pep_oracle.server.handler` behind CloudFront+OAC over a Function URL, least-privilege IAM). Lambda env encodes the runtime contract (`SERVE_FROM_ARTIFACT=1`, `EMBED_BACKEND=bedrock`, `OAUTH_STORE=dynamodb`, `OAUTH_SIGNING_BACKEND=ssm`, `AUTHORIZE_GATE=cognito`). Tests: `cd infra && .venv/bin/python -m pytest` (excluded from root pytest via `--ignore=infra`). Deploy/cutover: `docs/aws/phase2c-deploy-runbook.md` (not yet executed).
```

And add one line under `## Architecture` near the serving-seam description, or to the OAuth design bullet, noting the Lambda serves stateless: append to the MCP-server bullet "; serves stateless (`mcp` `stateless_http=True`) so any Lambda container handles any request."

Keep `CLAUDE.md` under 300 lines (`wc -l CLAUDE.md`).

- [ ] **Step 4: Verify root tests + CLAUDE.md size**

Run: `uv run pytest -q && wc -l CLAUDE.md`
Expected: PASS (root suite unaffected; infra ignored); CLAUDE.md < 300.

- [ ] **Step 5: Run `/claude-md-improver` and commit**

Run `/claude-md-improver` (required by the commit hook because this task edits `CLAUDE.md`), then:

```bash
touch .claude/.md-reviewed
git add docs/aws/phase2c-deploy-runbook.md .env.example CLAUDE.md
git commit -m "docs(phase2c): deploy/cutover runbook, env, CLAUDE.md"
```

---

## Self-Review

**Spec coverage** (against `docs/superpowers/specs/2026-06-02-aws-mcp-migration-design.md` Sections 1, 3 (IaC/CDK), 4, and the resolved open questions):

| Spec requirement | Task |
|---|---|
| §1 serving = scale-to-zero Lambda (FastAPI+Mangum), same app local/CI/prod | Task 3 (image), Task 6 (`DockerImageFunction`, `pep_oracle.server.handler`) |
| §1 MCP `stateless_http=True` for multi-user | Task 1 |
| §1 reserved-concurrency cap | Task 6 (`reserved_concurrent_executions=30`) |
| §1/§2 serve from versioned S3 corpus artifact | Task 4 (bucket), Task 6 (`SERVE_FROM_ARTIFACT=1`, `CORPUS_URI`), Task 8 (publish) |
| §3 IaC = AWS CDK (Python) | Tasks 2-7 |
| §3 container-image Lambda (not zip) | Task 3, Task 6 |
| §4.1 CloudFront → Function URL locked by OAC; custom domain + ACM | Task 7 |
| §4.3 OAuth signing = HS256 from SSM (the 2b2 seam) | Task 6 env + IAM, Task 8 param creation |
| §4.4 least-privilege IAM (bedrock one model, s3 read, ddb RW, ssm get, kms decrypt) | Task 6 |
| §4.4 no Lambda in a VPC | Task 6 (no VPC configured) |
| §4.5 encryption at rest (S3 + DynamoDB KMS); bucket private+versioned | Task 4 |
| §4.6 Cognito one-user identity gate | Task 5 (pool/client/domain), Task 6 (`AUTHORIZE_GATE=cognito` + COGNITO_* env), Task 8 (user) |
| Open-Q: cert in us-east-1, compute in ap-southeast-2 | Task 7 (two stacks, cross-region cert) |
| Open-Q: reuse pep-oracle.iicapn.com via Route 53, direct cutover | Task 7 (zone+alias), Task 8 (delegation+cutover) |
| Out of scope: staging, CI/CD, Fargate ingestion, KMS-asymmetric, WAF | not in this plan (Phases 3/4/5) |

**Placeholder scan:** every code/CDK step shows complete code; the one intentional human value is `allowed_email` (context, flagged `REPLACE_ME`). No TBD/"handle errors"/"similar to". ✔

**Type/name consistency:**
- `DeployConfig` fields (`domain_name`, `compute_region`, `cert_region`, `corpus_bucket_name`, `cognito_domain_prefix`, `allowed_email`, `embed_model`, `embed_dims`, `oauth_table_name`, `signing_ssm_param`, `public_url`) defined Task 2, used Tasks 4-7 consistently. ✔
- `PepOracleProdStack(cfg, cert_arn, hosted_zone_id, hosted_zone_name)` — signature defined Task 2, dummy values in the test helper (Task 4), real wiring in `app.py` (Task 7). ✔
- DynamoDB schema (`pk`, `family-index`/`family_id`, `ttl`, `PAY_PER_REQUEST`, KEYS_ONLY) matches `oauth_store.DynamoDbStore` exactly (verified). ✔
- Lambda env keys match `src/pep_oracle/config.py` env-var names exactly (`PEP_ORACLE_*`), incl. `PEP_ORACLE_PUBLIC_URL` read in `server.py`. ✔
- Cognito callback `…/oauth/authorize/callback` matches `authorize_gate.CALLBACK_PATH`. ✔
- `self.fn_url` (Task 6) consumed by `FunctionUrlOrigin.with_origin_access_control` (Task 7). ✔

**Verified in-env before planning:** repo facts via code read (DynamoDB schema, env vars, `server.handler` Mangum, `stateless_http` gap, `aws`/`server` extras already present); current CDK Python API via the official API reference (Function-URL OAC origin, cross-region ACM cert, Cognito constructs).

**Deploy-time caveats deliberately deferred to the runbook (not author-time blockers):** `cdk bootstrap`, the SSM SecureString param value, Cognito user creation, corpus upload, NS subdomain delegation, the live cutover, and the Docker image build (`DockerImageCode.from_image_asset` only builds on real `cdk deploy`, not during assertion-test synth).
