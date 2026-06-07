# Phase 2b2 — SSM Signing Backend + Cognito Authorize Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the two app-level seams the AWS serving Lambda needs — a pluggable OAuth signing-key backend (HS256 from SSM SecureString) and an in-app Cognito identity gate on `/oauth/authorize` — both behind config selectors that leave the OptiPlex/local defaults unchanged.

**Architecture:** Two new modules mirror the existing `oauth_store.py` backend-seam pattern. `signing.py` resolves the HS256 secret via `local` (env→file→generate, today's behavior) or `ssm` backends. `authorize_gate.py` provides an `AuthorizeGate` seam with `TrustedUpstreamGate` (auto-approve, today's behavior) and `CognitoGate` (brokers a login through a one-user Cognito Hosted UI: bounce browser → callback exchanges the code → verify the ID token via the pool JWKS + an email allow-list → issue the pep-oracle code). The Cognito leg is stateless: the original MCP authorize params ride to the callback inside a short-lived HS256 "login-state" JWT signed with the existing signing key — no new store rows, no session cookies. `oauth.py` consults the injected gate; `server.mount_mcp_if_configured` selects the gate from config and relaxes the `TRUSTS_UPSTREAM_AUTH` mount guard when the in-app Cognito gate is the auth.

**Tech Stack:** FastAPI, PyJWT (`pyjwt[crypto]`, RS256/JWK already verified in-env), `requests` (Cognito token exchange), boto3 SSM, moto (SSM + DynamoDB mocks), pytest + TestClient.

**Out of scope (later phases):** the CDK that provisions the SSM param + Cognito pool (Phase 2c); KMS asymmetric signing (Phase 5). This plan ships the *app* seams; the real AWS resources are stood up by the 2c runbook/CDK.

**Commit hook (every task):** the repo's `PreToolUse` hook blocks `git commit` unless `pytest -x -q` passes **and** `/claude-md-improver` has been run with `.claude/.md-reviewed` touched. Stage `CLAUDE.md` in the same commit when you change it (Task 7). Each task's commit step assumes a green `pytest`.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `src/pep_oracle/config.py` | New env-backed config constants (signing backend, Cognito gate) | 1, 2 |
| `src/pep_oracle/signing.py` (new) | Pluggable signing-key resolver: `local` + `ssm` backends | 1 |
| `src/pep_oracle/server.py` | `_resolve_signing_key` delegates to `signing`; mount selects gate + relaxes guard | 1, 6 |
| `src/pep_oracle/authorize_gate.py` (new) | `AuthorizeGate` seam: `TrustedUpstreamGate`, `CognitoGate`, `get_gate()` | 2, 3, 4 |
| `src/pep_oracle/oauth.py` | login-state JWT helpers; gate-aware `/oauth/authorize` + `/oauth/authorize/callback` | 5 |
| `tests/test_signing.py` (new) | signing backend unit tests | 1 |
| `tests/test_authorize_gate.py` (new) | gate selection, Cognito URL build, ID-token verify, code exchange | 2, 3, 4 |
| `tests/test_oauth.py` | authorize/callback route behavior with a fake gate | 5 |
| `tests/test_server.py` | mount wiring with the gate | 6 |
| `docs/aws/phase2b2-signing-and-cognito.md` (new) | operator runbook (SSM param + Cognito pool setup) | 7 |
| `CLAUDE.md`, `.env.example` | document the new env vars + seam behavior | 7 |

---

## Task 1: Pluggable signing-key backend (`signing.py`)

Move the existing key-resolution logic out of `server.py` into a `signing` module with a `local` backend (unchanged behavior) and an `ssm` backend (HS256 SecureString). `server._resolve_signing_key` becomes a one-line delegator so the mount call site and the existing monkeypatch test keep working.

**Files:**
- Create: `src/pep_oracle/signing.py`
- Modify: `src/pep_oracle/config.py` (add three constants)
- Modify: `src/pep_oracle/server.py:5` (drop `import secrets`), `src/pep_oracle/server.py:154-174` (delegate)
- Test: `tests/test_signing.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_signing.py`:

```python
"""Tests for the pluggable OAuth signing-key backend (signing.py)."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from pep_oracle import config, signing


def test_local_backend_prefers_env_var(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OAUTH_SIGNING_BACKEND", "local")
    monkeypatch.setenv("PEP_ORACLE_OAUTH_SIGNING_KEY", "env-key-value")
    monkeypatch.setenv("PEP_ORACLE_DATA_DIR", str(tmp_path))
    assert signing.resolve_signing_key() == "env-key-value"


def test_local_backend_generates_and_persists_key(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OAUTH_SIGNING_BACKEND", "local")
    monkeypatch.delenv("PEP_ORACLE_OAUTH_SIGNING_KEY", raising=False)
    monkeypatch.setenv("PEP_ORACLE_DATA_DIR", str(tmp_path))
    first = signing.resolve_signing_key()
    assert first  # non-empty
    key_file = tmp_path / "oauth_signing_key"
    assert key_file.exists()
    assert oct(key_file.stat().st_mode)[-3:] == "600"
    # second call reads the same persisted key
    assert signing.resolve_signing_key() == first


def test_ssm_backend_returns_securestring(monkeypatch):
    monkeypatch.setattr(config, "OAUTH_SIGNING_BACKEND", "ssm")
    monkeypatch.setattr(config, "OAUTH_SIGNING_SSM_PARAM", "/pep-oracle/oauth-signing-key")
    monkeypatch.setattr(config, "OAUTH_SIGNING_SSM_REGION", "ap-southeast-2")
    with mock_aws():
        ssm = boto3.client("ssm", region_name="ap-southeast-2")
        ssm.put_parameter(
            Name="/pep-oracle/oauth-signing-key",
            Value="ssm-secret-32-bytes-long-xxxxxxxxxxxx",
            Type="SecureString",
        )
        assert signing.resolve_signing_key() == "ssm-secret-32-bytes-long-xxxxxxxxxxxx"


def test_ssm_backend_missing_param_raises(monkeypatch):
    """Fail-closed: a missing SSM param must raise, never silently generate a key
    (which would mismatch every previously issued token)."""
    monkeypatch.setattr(config, "OAUTH_SIGNING_BACKEND", "ssm")
    monkeypatch.setattr(config, "OAUTH_SIGNING_SSM_PARAM", "/pep-oracle/does-not-exist")
    monkeypatch.setattr(config, "OAUTH_SIGNING_SSM_REGION", "ap-southeast-2")
    with mock_aws():
        boto3.client("ssm", region_name="ap-southeast-2")  # ensure region exists in moto
        with pytest.raises(Exception):
            signing.resolve_signing_key()


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setattr(config, "OAUTH_SIGNING_BACKEND", "kms-not-yet")
    with pytest.raises(ValueError):
        signing.resolve_signing_key()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_signing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pep_oracle.signing'` (and missing config attrs).

- [ ] **Step 3: Add config constants**

In `src/pep_oracle/config.py`, after the `OAUTH_DDB_REGION` block (line 60), add:

```python

# --- OAuth signing-key backend (Phase 2b2) ---
# "local" (default): env PEP_ORACLE_OAUTH_SIGNING_KEY -> $DATA_DIR/oauth_signing_key
# -> a freshly generated 0600 key (unchanged OptiPlex/dev behavior). "ssm": an
# HS256 SecureString from SSM Parameter Store (the Lambda path).
OAUTH_SIGNING_BACKEND = os.getenv("PEP_ORACLE_OAUTH_SIGNING_BACKEND", "local")
OAUTH_SIGNING_SSM_PARAM = os.getenv(
    "PEP_ORACLE_OAUTH_SIGNING_SSM_PARAM", "/pep-oracle/oauth-signing-key"
)
OAUTH_SIGNING_SSM_REGION = os.getenv("PEP_ORACLE_OAUTH_SIGNING_SSM_REGION", BEDROCK_REGION)
```

- [ ] **Step 4: Create `signing.py`**

Create `src/pep_oracle/signing.py`:

```python
"""Pluggable OAuth signing-key backend (Phase 2b2).

Resolves the HS256 secret used to sign/verify access-token JWTs. Two backends
behind one ``resolve_signing_key()`` entry point, selected by config:
  - "local" (default): env ``PEP_ORACLE_OAUTH_SIGNING_KEY`` -> ``$DATA_DIR/oauth_signing_key``
    -> a freshly generated key written 0600. Unchanged OptiPlex/dev behavior.
  - "ssm": a KMS-encrypted SecureString from SSM Parameter Store (the Lambda path).
    Fail-closed -- a missing/empty parameter raises rather than silently generating
    a key that would mismatch every previously issued token.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from pep_oracle import config

logger = logging.getLogger(__name__)


def resolve_signing_key() -> str:
    backend = config.OAUTH_SIGNING_BACKEND
    if backend == "ssm":
        return _resolve_ssm(config.OAUTH_SIGNING_SSM_PARAM, config.OAUTH_SIGNING_SSM_REGION)
    if backend == "local":
        return _resolve_local()
    raise ValueError(f"unknown PEP_ORACLE_OAUTH_SIGNING_BACKEND: {backend!r}")


def _resolve_local() -> str:
    """env PEP_ORACLE_OAUTH_SIGNING_KEY -> $DATA_DIR/oauth_signing_key -> generated 0600."""
    env_key = os.environ.get("PEP_ORACLE_OAUTH_SIGNING_KEY", "").strip()
    if env_key:
        return env_key
    data_dir = Path(
        os.environ.get("PEP_ORACLE_DATA_DIR") or (Path.home() / ".pep-oracle")
    ).expanduser()
    key_path = data_dir / "oauth_signing_key"
    if key_path.exists():
        existing = key_path.read_text().strip()
        if existing:
            return existing
    data_dir.mkdir(parents=True, exist_ok=True)
    new_key = secrets.token_urlsafe(32)
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, new_key.encode("ascii"))
    finally:
        os.close(fd)
    logger.info("Generated new OAuth signing key at %s (mode 0600)", key_path)
    return new_key


def _resolve_ssm(param_name: str, region: str) -> str:
    import boto3

    ssm = boto3.client("ssm", region_name=region)
    resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
    value = resp["Parameter"]["Value"].strip()
    if not value:
        raise RuntimeError(f"SSM signing-key parameter {param_name!r} is empty")
    logger.info("Loaded OAuth signing key from SSM parameter %s", param_name)
    return value
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_signing.py -q`
Expected: PASS (5 passed).

- [ ] **Step 6: Delegate `server._resolve_signing_key` to `signing`**

In `src/pep_oracle/server.py`, replace the whole `_resolve_signing_key` function (lines 154–174) with:

```python
def _resolve_signing_key() -> str:
    """Resolve the OAuth HS256 signing key via the pluggable backend.

    Kept as a module-level seam so ``mount_mcp_if_configured`` and tests can patch it.
    """
    from pep_oracle import signing

    return signing.resolve_signing_key()
```

Then remove the now-unused `import secrets` at `src/pep_oracle/server.py:5` (it was only used by the moved generation logic; `ruff` will flag it as F401 otherwise). Leave `from pathlib import Path` — still used by `WEB_DIR`.

- [ ] **Step 7: Run the full suite to verify nothing regressed**

Run: `uv run pytest tests/test_signing.py tests/test_server.py tests/test_oauth.py -q`
Expected: PASS — in particular `test_mount_builds_oauth_store_from_config` (which patches `server._resolve_signing_key`) still passes, and no `ruff` unused-import failure.

- [ ] **Step 8: Commit**

```bash
git add src/pep_oracle/signing.py src/pep_oracle/config.py src/pep_oracle/server.py tests/test_signing.py
git commit -m "feat(oauth): pluggable signing-key backend (local | SSM SecureString)"
```

---

## Task 2: Authorize-gate seam + Cognito config & login redirect (`authorize_gate.py`)

Create the gate module: the `AuthorizeGate` protocol, `TrustedUpstreamGate` (today's auto-approve), the `CognitoGate` shell (construction from config, `requires_identity`, `issuer`, `login_redirect` URL builder), and the `get_gate()` selector. No network calls yet — those land in Tasks 3–4.

**Files:**
- Create: `src/pep_oracle/authorize_gate.py`
- Modify: `src/pep_oracle/config.py` (add gate + Cognito constants)
- Test: `tests/test_authorize_gate.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_authorize_gate.py`:

```python
"""Tests for the /oauth/authorize identity gate (authorize_gate.py)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from pep_oracle import authorize_gate, config
from pep_oracle.authorize_gate import CognitoGate, TrustedUpstreamGate


def _gate(**over) -> CognitoGate:
    kw = dict(
        domain="https://pep-oracle.auth.ap-southeast-2.amazoncognito.com",
        client_id="cognito-client-x",
        client_secret="cognito-secret",
        user_pool_id="ap-southeast-2_pool",
        region="ap-southeast-2",
        allowed_emails=["me@example.com"],
    )
    kw.update(over)
    return CognitoGate(**kw)


def test_get_gate_defaults_to_trusted_upstream(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "trusted_upstream")
    gate = authorize_gate.get_gate()
    assert isinstance(gate, TrustedUpstreamGate)
    assert gate.requires_identity() is False


def test_get_gate_cognito_builds_from_config(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "cognito")
    monkeypatch.setattr(config, "COGNITO_DOMAIN", "https://d.example")
    monkeypatch.setattr(config, "COGNITO_CLIENT_ID", "cid")
    monkeypatch.setattr(config, "COGNITO_CLIENT_SECRET", "sec")
    monkeypatch.setattr(config, "COGNITO_USER_POOL_ID", "ap-southeast-2_pool")
    monkeypatch.setattr(config, "COGNITO_REGION", "ap-southeast-2")
    monkeypatch.setattr(config, "COGNITO_ALLOWED_EMAILS", "me@example.com")
    gate = authorize_gate.get_gate()
    assert isinstance(gate, CognitoGate)
    assert gate.requires_identity() is True
    assert gate.allowed_emails == ["me@example.com"]


def test_cognito_from_config_missing_fields_raises(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "cognito")
    monkeypatch.setattr(config, "COGNITO_DOMAIN", "")
    monkeypatch.setattr(config, "COGNITO_CLIENT_ID", "")
    monkeypatch.setattr(config, "COGNITO_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "COGNITO_USER_POOL_ID", "")
    monkeypatch.setattr(config, "COGNITO_ALLOWED_EMAILS", "")
    with pytest.raises(ValueError):
        authorize_gate.get_gate()


def test_cognito_issuer_url():
    assert _gate().issuer == (
        "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_pool"
    )


def test_cognito_login_redirect_url():
    url = _gate().login_redirect(
        redirect_uri="https://pep-oracle.example/oauth/authorize/callback",
        login_state="LOGIN_STATE_BLOB",
    )
    parsed = urlparse(url)
    assert parsed.netloc == "pep-oracle.auth.ap-southeast-2.amazoncognito.com"
    assert parsed.path == "/oauth2/authorize"
    qs = parse_qs(parsed.query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["cognito-client-x"]
    assert qs["redirect_uri"] == ["https://pep-oracle.example/oauth/authorize/callback"]
    assert qs["state"] == ["LOGIN_STATE_BLOB"]
    assert "openid" in qs["scope"][0] and "email" in qs["scope"][0]


def test_allowed_emails_normalized_lowercased():
    gate = _gate(allowed_emails=["  Me@Example.com ", "", "Two@x.com"])
    assert gate.allowed_emails == ["me@example.com", "two@x.com"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_authorize_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'pep_oracle.authorize_gate'`.

- [ ] **Step 3: Add config constants**

In `src/pep_oracle/config.py`, after the signing-backend block added in Task 1, add:

```python

# --- /oauth/authorize identity gate (Phase 2b2) ---
# "trusted_upstream" (default): auto-approve, relying on an upstream authenticator
# (Cloudflare Access) -- the OptiPlex model. "cognito": in-app identity check against
# a one-user Cognito user pool (the AWS model; no external-edge dependency).
AUTHORIZE_GATE = os.getenv("PEP_ORACLE_AUTHORIZE_GATE", "trusted_upstream")
# Hosted-UI base, e.g. https://pep-oracle.auth.ap-southeast-2.amazoncognito.com
COGNITO_DOMAIN = os.getenv("PEP_ORACLE_COGNITO_DOMAIN", "")
COGNITO_CLIENT_ID = os.getenv("PEP_ORACLE_COGNITO_CLIENT_ID", "")
COGNITO_CLIENT_SECRET = os.getenv("PEP_ORACLE_COGNITO_CLIENT_SECRET", "")
COGNITO_USER_POOL_ID = os.getenv("PEP_ORACLE_COGNITO_USER_POOL_ID", "")  # e.g. ap-southeast-2_abc123
COGNITO_REGION = os.getenv("PEP_ORACLE_COGNITO_REGION", BEDROCK_REGION)
COGNITO_ALLOWED_EMAILS = os.getenv("PEP_ORACLE_COGNITO_ALLOWED_EMAILS", "")  # comma-separated
```

- [ ] **Step 4: Create `authorize_gate.py` (shell + Cognito URL build)**

Create `src/pep_oracle/authorize_gate.py`:

```python
"""/oauth/authorize identity gate (Phase 2b2).

Two gates behind one ``AuthorizeGate`` protocol, selected by ``config.AUTHORIZE_GATE``:
  - ``TrustedUpstreamGate`` ("trusted_upstream", default): no in-app identity check;
    /oauth/authorize auto-approves, relying on an upstream authenticator (Cloudflare
    Access). The OptiPlex model -- unchanged behavior.
  - ``CognitoGate`` ("cognito"): /oauth/authorize brokers a login through a one-user
    Cognito user pool (Hosted UI). The browser is bounced to Cognito; the callback
    exchanges the code, verifies the ID token (RS256 via the pool JWKS) and the
    caller's email against an allow-list, then issues the pep-oracle auth code.
    Removes the external-edge dependency and the fail-open-if-misconfigured risk.

Code exchange (`_exchange_code`) and ID-token verification (`_verify_id_token`) land
in later steps; this module starts with construction + the login-redirect URL.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol
from urllib.parse import urlencode

from pep_oracle import config

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/oauth/authorize/callback"
_HTTP_TIMEOUT = 15


class IdentityError(Exception):
    """Cognito login/verification failed. Opaque -- don't leak which check failed."""


class AuthorizeGate(Protocol):
    def requires_identity(self) -> bool: ...


class TrustedUpstreamGate:
    """No in-app identity check (relies on an upstream authenticator)."""

    def requires_identity(self) -> bool:
        return False


class CognitoGate:
    def __init__(
        self,
        *,
        domain: str,
        client_id: str,
        client_secret: str,
        user_pool_id: str,
        region: str,
        allowed_emails: list[str],
    ) -> None:
        self.domain = domain.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_pool_id = user_pool_id
        self.region = region
        self.allowed_emails = [e.strip().lower() for e in allowed_emails if e.strip()]
        self._jwks_cache: Optional[dict[str, Any]] = None

    @classmethod
    def from_config(cls) -> "CognitoGate":
        missing = [
            name
            for name, val in (
                ("PEP_ORACLE_COGNITO_DOMAIN", config.COGNITO_DOMAIN),
                ("PEP_ORACLE_COGNITO_CLIENT_ID", config.COGNITO_CLIENT_ID),
                ("PEP_ORACLE_COGNITO_CLIENT_SECRET", config.COGNITO_CLIENT_SECRET),
                ("PEP_ORACLE_COGNITO_USER_POOL_ID", config.COGNITO_USER_POOL_ID),
                ("PEP_ORACLE_COGNITO_ALLOWED_EMAILS", config.COGNITO_ALLOWED_EMAILS),
            )
            if not val
        ]
        if missing:
            raise ValueError("AUTHORIZE_GATE=cognito requires: " + ", ".join(missing))
        return cls(
            domain=config.COGNITO_DOMAIN,
            client_id=config.COGNITO_CLIENT_ID,
            client_secret=config.COGNITO_CLIENT_SECRET,
            user_pool_id=config.COGNITO_USER_POOL_ID,
            region=config.COGNITO_REGION,
            allowed_emails=config.COGNITO_ALLOWED_EMAILS.split(","),
        )

    def requires_identity(self) -> bool:
        return True

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    def login_redirect(self, *, redirect_uri: str, login_state: str) -> str:
        """Cognito Hosted UI authorize URL to bounce the browser to."""
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email",
            "state": login_state,
        }
        return f"{self.domain}/oauth2/authorize?{urlencode(params)}"


def get_gate() -> AuthorizeGate:
    if config.AUTHORIZE_GATE == "cognito":
        return CognitoGate.from_config()
    return TrustedUpstreamGate()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_authorize_gate.py -q`
Expected: PASS (6 passed).

- [ ] **Step 6: Commit**

```bash
git add src/pep_oracle/authorize_gate.py src/pep_oracle/config.py tests/test_authorize_gate.py
git commit -m "feat(oauth): authorize-gate seam + Cognito login-redirect URL"
```

---

## Task 3: Cognito ID-token verification (`_verify_id_token`)

Add JWKS-based RS256 verification of the Cognito ID token plus the email allow-list check — the core security primitive, isolated and fully unit-testable with a locally generated RSA key (no real Cognito).

**Files:**
- Modify: `src/pep_oracle/authorize_gate.py` (imports + `_fetch_jwks` + `_verify_id_token`)
- Test: `tests/test_authorize_gate.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_authorize_gate.py`. First, add these imports at the top of the file (next to the existing imports):

```python
import json
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
```

Then append:

```python
# --- ID-token verification ------------------------------------------------

_KID = "test-kid-1"


def _rsa_keypair():
    """Return (private_pem_str, jwks_dict) for signing/verifying test ID tokens."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": _KID, "alg": "RS256", "use": "sig"})
    return priv_pem, {"keys": [jwk]}


def _id_token(priv_pem, *, iss, aud, email, ttl=3600, kid=_KID):
    now = int(time.time())
    claims = {"sub": "u1", "iss": iss, "aud": aud, "email": email,
              "iat": now, "exp": now + ttl}
    return jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": kid})


def _verifying_gate(monkeypatch):
    priv_pem, jwks = _rsa_keypair()
    gate = _gate()
    monkeypatch.setattr(gate, "_fetch_jwks", lambda: jwks)
    return gate, priv_pem


def test_verify_id_token_accepts_valid(monkeypatch):
    gate, priv_pem = _verifying_gate(monkeypatch)
    tok = _id_token(priv_pem, iss=gate.issuer, aud=gate.client_id, email="me@example.com")
    claims = gate._verify_id_token(tok)
    assert claims["email"] == "me@example.com"


def test_verify_id_token_rejects_disallowed_email(monkeypatch):
    gate, priv_pem = _verifying_gate(monkeypatch)
    tok = _id_token(priv_pem, iss=gate.issuer, aud=gate.client_id, email="intruder@evil.com")
    with pytest.raises(authorize_gate.IdentityError):
        gate._verify_id_token(tok)


def test_verify_id_token_rejects_wrong_audience(monkeypatch):
    gate, priv_pem = _verifying_gate(monkeypatch)
    tok = _id_token(priv_pem, iss=gate.issuer, aud="some-other-client", email="me@example.com")
    with pytest.raises(authorize_gate.IdentityError):
        gate._verify_id_token(tok)


def test_verify_id_token_rejects_wrong_issuer(monkeypatch):
    gate, priv_pem = _verifying_gate(monkeypatch)
    tok = _id_token(priv_pem, iss="https://evil.example", aud=gate.client_id, email="me@example.com")
    with pytest.raises(authorize_gate.IdentityError):
        gate._verify_id_token(tok)


def test_verify_id_token_rejects_expired(monkeypatch):
    gate, priv_pem = _verifying_gate(monkeypatch)
    tok = _id_token(priv_pem, iss=gate.issuer, aud=gate.client_id, email="me@example.com", ttl=-10)
    with pytest.raises(authorize_gate.IdentityError):
        gate._verify_id_token(tok)


def test_verify_id_token_rejects_unknown_kid(monkeypatch):
    gate, priv_pem = _verifying_gate(monkeypatch)
    tok = _id_token(priv_pem, iss=gate.issuer, aud=gate.client_id, email="me@example.com", kid="other-kid")
    with pytest.raises(authorize_gate.IdentityError):
        gate._verify_id_token(tok)


def test_verify_id_token_rejects_bad_signature(monkeypatch):
    gate, _ = _verifying_gate(monkeypatch)
    other_priv, _ = _rsa_keypair()  # signed by a key NOT in the gate's JWKS
    tok = _id_token(other_priv, iss=gate.issuer, aud=gate.client_id, email="me@example.com")
    with pytest.raises(authorize_gate.IdentityError):
        gate._verify_id_token(tok)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_authorize_gate.py -k verify_id_token -q`
Expected: FAIL — `AttributeError: 'CognitoGate' object has no attribute '_verify_id_token'`.

- [ ] **Step 3: Implement `_fetch_jwks` + `_verify_id_token`**

In `src/pep_oracle/authorize_gate.py`, extend the top-of-file imports to:

```python
import json
import logging
from typing import Any, Optional, Protocol
from urllib.parse import urlencode

import jwt
import requests
from jwt.algorithms import RSAAlgorithm

from pep_oracle import config
```

Then add these two methods to `CognitoGate` (e.g. directly after `login_redirect`):

```python
    def _fetch_jwks(self) -> dict[str, Any]:
        """Fetch (and cache) the pool's JWKS. One fetch per warm container."""
        if self._jwks_cache is None:
            resp = requests.get(
                f"{self.issuer}/.well-known/jwks.json", timeout=_HTTP_TIMEOUT
            )
            resp.raise_for_status()
            self._jwks_cache = resp.json()
        return self._jwks_cache

    def _verify_id_token(self, id_token: str) -> dict[str, Any]:
        """Verify the Cognito ID token (RS256 via pool JWKS) and the email allow-list.

        Raises IdentityError on any failure (bad sig/iss/aud/exp, unknown kid, or a
        caller whose email is not on the allow-list).
        """
        try:
            header = jwt.get_unverified_header(id_token)
            jwks = self._fetch_jwks()
            jwk = next(
                (k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")),
                None,
            )
            if jwk is None:
                raise IdentityError("no matching JWKS key")
            public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))
            claims = jwt.decode(
                id_token,
                public_key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except IdentityError:
            raise
        except Exception as e:  # noqa: BLE001 -- single opaque error per spec
            raise IdentityError("id_token verification failed") from e
        email = str(claims.get("email", "")).lower()
        if email not in self.allowed_emails:
            logger.warning("Cognito login rejected: email not on allow-list")
            raise IdentityError("email not allowed")
        return claims
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_authorize_gate.py -q`
Expected: PASS (all gate tests, including the 7 new verify tests).

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/authorize_gate.py tests/test_authorize_gate.py
git commit -m "feat(oauth): Cognito ID-token verification (JWKS RS256 + email allow-list)"
```

---

## Task 4: Cognito code exchange + `exchange_and_verify`

Add the Hosted-UI authorization-code exchange (HTTP POST to the Cognito token endpoint) and the `exchange_and_verify` convenience that composes exchange + verify — the single method `oauth.py` will call from the callback route.

**Files:**
- Modify: `src/pep_oracle/authorize_gate.py` (`_exchange_code` + `exchange_and_verify`)
- Test: `tests/test_authorize_gate.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_authorize_gate.py`:

```python
# --- code exchange + exchange_and_verify ----------------------------------


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_exchange_and_verify_happy_path(monkeypatch):
    gate, priv_pem = _verifying_gate(monkeypatch)
    tok = _id_token(priv_pem, iss=gate.issuer, aud=gate.client_id, email="me@example.com")
    captured = {}

    def fake_post(url, data=None, auth=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["auth"] = auth
        return _FakeResp(200, {"id_token": tok})

    monkeypatch.setattr(authorize_gate.requests, "post", fake_post)
    claims = gate.exchange_and_verify(code="cognito-code", redirect_uri="https://app/cb")
    assert claims["email"] == "me@example.com"
    assert captured["url"].endswith("/oauth2/token")
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "cognito-code"
    assert captured["data"]["redirect_uri"] == "https://app/cb"
    assert captured["auth"] == (gate.client_id, gate.client_secret)


def test_exchange_non_200_raises(monkeypatch):
    gate = _gate()
    monkeypatch.setattr(
        authorize_gate.requests, "post",
        lambda *a, **k: _FakeResp(400, {"error": "invalid_grant"}),
    )
    with pytest.raises(authorize_gate.IdentityError):
        gate.exchange_and_verify(code="bad", redirect_uri="https://app/cb")


def test_exchange_missing_id_token_raises(monkeypatch):
    gate = _gate()
    monkeypatch.setattr(
        authorize_gate.requests, "post",
        lambda *a, **k: _FakeResp(200, {"access_token": "a"}),  # no id_token
    )
    with pytest.raises(authorize_gate.IdentityError):
        gate.exchange_and_verify(code="x", redirect_uri="https://app/cb")


def test_exchange_network_error_raises(monkeypatch):
    gate = _gate()

    def boom(*a, **k):
        raise authorize_gate.requests.RequestException("connreset")

    monkeypatch.setattr(authorize_gate.requests, "post", boom)
    with pytest.raises(authorize_gate.IdentityError):
        gate.exchange_and_verify(code="x", redirect_uri="https://app/cb")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_authorize_gate.py -k exchange -q`
Expected: FAIL — `AttributeError: 'CognitoGate' object has no attribute 'exchange_and_verify'`.

- [ ] **Step 3: Implement `_exchange_code` + `exchange_and_verify`**

In `src/pep_oracle/authorize_gate.py`, add to `CognitoGate` (after `_verify_id_token`):

```python
    def exchange_and_verify(self, *, code: str, redirect_uri: str) -> dict[str, Any]:
        """Exchange a Cognito auth code, then verify the returned ID token.

        Returns the verified ID-token claims; raises IdentityError on any failure.
        ``redirect_uri`` must equal the one sent to the Hosted UI (Cognito enforces).
        """
        id_token = self._exchange_code(code=code, redirect_uri=redirect_uri)
        return self._verify_id_token(id_token)

    def _exchange_code(self, *, code: str, redirect_uri: str) -> str:
        try:
            resp = requests.post(
                f"{self.domain}/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": self.client_id,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                auth=(self.client_id, self.client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise IdentityError("token exchange failed") from e
        if resp.status_code != 200:
            logger.warning("Cognito token exchange non-200: %s", resp.status_code)
            raise IdentityError("token exchange rejected")
        id_token = resp.json().get("id_token")
        if not id_token:
            raise IdentityError("no id_token in token response")
        return id_token
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_authorize_gate.py -q`
Expected: PASS (all gate tests).

- [ ] **Step 5: Commit**

```bash
git add src/pep_oracle/authorize_gate.py tests/test_authorize_gate.py
git commit -m "feat(oauth): Cognito code exchange + exchange_and_verify"
```

---

## Task 5: Gate-aware `/oauth/authorize` + `/oauth/authorize/callback` in `oauth.py`

Wire the gate into the OAuth routes. When the gate requires identity, `/oauth/authorize` bounces the browser to the IdP carrying a signed login-state JWT (original MCP params), and a new `/oauth/authorize/callback` resumes the flow after a verified login. The existing auto-approve path (default `TrustedUpstreamGate`) is preserved unchanged. Tests use a fake gate so this task is independent of real Cognito.

**Files:**
- Modify: `src/pep_oracle/oauth.py` (imports, login-state helpers, `register_oauth_routes` signature, `authorize` body, new callback route)
- Test: `tests/test_oauth.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_oauth.py`:

```python
# --- Identity-gate authorize + callback (Phase 2b2) ----------------------

from pep_oracle.authorize_gate import IdentityError  # noqa: E402


class _FakeGate:
    """Stand-in for CognitoGate; no network. ``verify`` may return claims or raise."""

    def __init__(self, *, identity=True, verify=None):
        self._identity = identity
        self._verify = verify
        self.exchange_calls: list[tuple[str, str]] = []
        self.login_url = "https://cognito.example/oauth2/authorize"

    def requires_identity(self) -> bool:
        return self._identity

    def login_redirect(self, *, redirect_uri: str, login_state: str) -> str:
        from urllib.parse import urlencode
        return f"{self.login_url}?{urlencode({'redirect_uri': redirect_uri, 'state': login_state})}"

    def exchange_and_verify(self, *, code: str, redirect_uri: str) -> dict:
        self.exchange_calls.append((code, redirect_uri))
        if self._verify is None:
            return {"email": "me@example.com"}
        return self._verify(code, redirect_uri)


def _client_with_gate(gate):
    app = FastAPI()
    store = oauth_store.SqliteStore(":memory:")
    register_oauth_routes(app, SIGNING_KEY, PUBLIC_URL, store, gate)
    return TestClient(app), store


def test_authorize_with_identity_gate_redirects_to_idp():
    gate = _FakeGate(identity=True)
    client, store = _client_with_gate(gate)
    client_id = _register(client)
    _, challenge = _pkce_pair()
    r = _authorize(client, client_id, challenge, state="client-state-xyz")
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert loc.netloc == "cognito.example"
    qs = parse_qs(loc.query)
    # callback URI points back at this server
    assert qs["redirect_uri"] == [f"{PUBLIC_URL}/oauth/authorize/callback"]
    # the login_state encodes the original MCP request and verifies under SIGNING_KEY
    ls = oauth._decode_login_state(SIGNING_KEY, PUBLIC_URL, qs["state"][0])
    assert ls["mcp_client_id"] == client_id
    assert ls["mcp_redirect_uri"] == "https://claude.ai/cb"
    assert ls["mcp_code_challenge"] == challenge
    assert ls["mcp_state"] == "client-state-xyz"


def test_callback_issues_pep_code_after_successful_identity():
    gate = _FakeGate(identity=True)  # default verify -> allowed identity
    client, store = _client_with_gate(gate)
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    login_state = oauth._encode_login_state(
        SIGNING_KEY, PUBLIC_URL, client_id=client_id,
        redirect_uri="https://claude.ai/cb", code_challenge=challenge,
        client_state="client-state-xyz",
    )
    r = client.get(
        "/oauth/authorize/callback",
        params={"code": "cognito-code", "state": login_state},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert gate.exchange_calls == [("cognito-code", f"{PUBLIC_URL}/oauth/authorize/callback")]
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://claude.ai/cb"
    qs = parse_qs(loc.query)
    assert qs["state"] == ["client-state-xyz"]
    code = qs["code"][0]
    # the issued code is a real pep-oracle auth code -> redeem it for tokens
    r2 = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "https://claude.ai/cb", "client_id": client_id,
        "code_verifier": verifier,
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["access_token"]


def test_callback_rejects_failed_identity():
    def deny(code, redirect_uri):
        raise IdentityError("nope")

    gate = _FakeGate(identity=True, verify=deny)
    client, store = _client_with_gate(gate)
    client_id = _register(client)
    _, challenge = _pkce_pair()
    login_state = oauth._encode_login_state(
        SIGNING_KEY, PUBLIC_URL, client_id=client_id,
        redirect_uri="https://claude.ai/cb", code_challenge=challenge, client_state=None,
    )
    r = client.get("/oauth/authorize/callback",
                   params={"code": "bad", "state": login_state}, follow_redirects=False)
    assert r.status_code == 403
    assert r.json()["error"] == "access_denied"


def test_callback_rejects_tampered_login_state():
    gate = _FakeGate(identity=True)
    client, _ = _client_with_gate(gate)
    r = client.get("/oauth/authorize/callback",
                   params={"code": "x", "state": "not-a-real-jwt"}, follow_redirects=False)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_callback_rejects_expired_login_state(monkeypatch):
    gate = _FakeGate(identity=True)
    client, _ = _client_with_gate(gate)
    client_id = _register(client)
    _, challenge = _pkce_pair()
    # mint a login_state that is already expired
    real = time.time()
    monkeypatch.setattr(oauth.time, "time", lambda: real - oauth.LOGIN_STATE_TTL_SECONDS - 10)
    login_state = oauth._encode_login_state(
        SIGNING_KEY, PUBLIC_URL, client_id=client_id,
        redirect_uri="https://claude.ai/cb", code_challenge=challenge, client_state=None,
    )
    monkeypatch.setattr(oauth.time, "time", lambda: real)
    r = client.get("/oauth/authorize/callback",
                   params={"code": "x", "state": login_state}, follow_redirects=False)
    assert r.status_code == 400


def test_callback_route_absent_for_trusted_upstream_gate(client):
    # the default `client` fixture registers routes WITHOUT a gate (trusted_upstream),
    # so the callback route must not exist.
    r = client.get("/oauth/authorize/callback", params={"code": "x", "state": "y"})
    assert r.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_oauth.py -k "identity or callback or login_state" -q`
Expected: FAIL — `AttributeError: module 'pep_oracle.oauth' has no attribute '_encode_login_state'` / `register_oauth_routes()` takes 4 positional args.

- [ ] **Step 3: Add imports + login-state helpers to `oauth.py`**

In `src/pep_oracle/oauth.py`, add to the imports (after the existing `from pep_oracle.oauth_store import ...` line):

```python
from pep_oracle.authorize_gate import CALLBACK_PATH, IdentityError, TrustedUpstreamGate
```

Add these module-level constants next to the existing ones (after `SCOPE = "mcp"`):

```python
LOGIN_STATE_AUDIENCE = "pep-oracle-login"
LOGIN_STATE_TTL_SECONDS = 600
```

Add these two helpers (e.g. just after `verify_access_token`):

```python
def _encode_login_state(
    signing_key: str,
    issuer: str,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    client_state: Optional[str],
) -> str:
    """Short-lived HS256 JWT carrying the original MCP authorize params across the
    Cognito leg (stateless -- no store row). Verified on the callback."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": LOGIN_STATE_AUDIENCE,
        "iat": now,
        "exp": now + LOGIN_STATE_TTL_SECONDS,
        "mcp_client_id": client_id,
        "mcp_redirect_uri": redirect_uri,
        "mcp_code_challenge": code_challenge,
    }
    if client_state is not None:
        claims["mcp_state"] = client_state
    return jwt.encode(claims, signing_key, algorithm="HS256")


def _decode_login_state(signing_key: str, issuer: str, blob: str) -> dict[str, Any]:
    """Verify + decode a login-state JWT. Raises on bad signature / iss / aud / exp."""
    return jwt.decode(
        blob,
        signing_key,
        algorithms=["HS256"],
        audience=LOGIN_STATE_AUDIENCE,
        issuer=issuer,
        options={"require": ["exp", "iat", "iss", "aud"]},
    )
```

- [ ] **Step 4: Add the `gate` param + an issue-code helper + gate-aware authorize**

In `src/pep_oracle/oauth.py`, change the `register_oauth_routes` signature (line 143-145) to:

```python
def register_oauth_routes(
    app: FastAPI,
    signing_key: str,
    public_url: str,
    store: OAuthStore,
    gate: Optional[AuthorizeGate] = None,
) -> None:
```

…and add this to the imports line from Task 5 Step 3 so `AuthorizeGate` is available:

```python
from pep_oracle.authorize_gate import (
    CALLBACK_PATH,
    AuthorizeGate,
    IdentityError,
    TrustedUpstreamGate,
)
```

Immediately after `issuer = public_url.rstrip("/")` inside `register_oauth_routes`, add:

```python
    if gate is None:
        gate = TrustedUpstreamGate()

    def _issue_code_and_redirect(
        *, client_id: str, redirect_uri: str, code_challenge: str, client_state: Optional[str]
    ) -> Response:
        code = secrets.token_urlsafe(32)
        store.put_auth_code(
            code, client_id=client_id, code_challenge=code_challenge,
            redirect_uri=redirect_uri, ttl_seconds=AUTH_CODE_TTL_SECONDS,
        )
        logger.info("authorize: issued code for client_id=%s", client_id)
        params = {"code": code}
        if client_state is not None:
            params["state"] = client_state
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(url=f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)
```

Then replace the tail of the `authorize` handler — the block from `code = secrets.token_urlsafe(32)` through the final `return RedirectResponse(...)` (lines 248-257) — with:

```python
        if gate.requires_identity():
            login_state = _encode_login_state(
                signing_key, issuer, client_id=client_id, redirect_uri=redirect_uri,
                code_challenge=code_challenge, client_state=state,
            )
            callback_uri = f"{issuer}{CALLBACK_PATH}"
            logger.info("authorize: redirecting to identity provider for client_id=%s", client_id)
            return RedirectResponse(
                url=gate.login_redirect(redirect_uri=callback_uri, login_state=login_state),
                status_code=302,
            )
        return _issue_code_and_redirect(
            client_id=client_id, redirect_uri=redirect_uri,
            code_challenge=code_challenge, client_state=state,
        )
```

- [ ] **Step 5: Add the callback route (only when the gate requires identity)**

In `src/pep_oracle/oauth.py`, register the callback route conditionally. Add this block inside `register_oauth_routes` after the `revoke` route definition (before the final `logger.info("OAuth provider routes registered ...")`):

```python
    if gate.requires_identity():

        @app.get(CALLBACK_PATH)
        async def authorize_callback(request: Request) -> Response:
            qp = request.query_params
            cognito_code = qp.get("code")
            login_state = qp.get("state")
            if qp.get("error"):
                logger.warning("authorize callback: idp error=%s", qp.get("error"))
                return _err(400, "access_denied", "identity provider returned an error")
            if not cognito_code or not login_state:
                return _err(400, "invalid_request", "missing code or state")
            try:
                ls = _decode_login_state(signing_key, issuer, login_state)
            except Exception:
                logger.warning("authorize callback: bad/expired login_state")
                return _err(400, "invalid_request", "invalid login state")

            mcp_client_id = ls["mcp_client_id"]
            mcp_redirect_uri = ls["mcp_redirect_uri"]
            mcp_code_challenge = ls["mcp_code_challenge"]
            mcp_state = ls.get("mcp_state")

            # Re-validate the client binding (it may have been deleted mid-flow).
            client = store.get_client(mcp_client_id)
            if client is None or mcp_redirect_uri not in client.redirect_uris:
                logger.warning("authorize callback: client/redirect no longer valid")
                return _err(400, "invalid_request", "client or redirect_uri no longer valid")

            callback_uri = f"{issuer}{CALLBACK_PATH}"
            try:
                gate.exchange_and_verify(code=cognito_code, redirect_uri=callback_uri)
            except IdentityError:
                logger.warning("authorize callback: identity verification failed")
                return _err(403, "access_denied", "identity verification failed")

            return _issue_code_and_redirect(
                client_id=mcp_client_id, redirect_uri=mcp_redirect_uri,
                code_challenge=mcp_code_challenge, client_state=mcp_state,
            )
```

Note: `gate.exchange_and_verify` is only defined on `CognitoGate`; this route is registered only when `requires_identity()` is True, so the attribute always exists here. The `AuthorizeGate` protocol intentionally stays minimal (just `requires_identity`); the callback is Cognito-specific.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_oauth.py -q`
Expected: PASS — the new identity/callback tests AND every pre-existing test (the default-gate path is unchanged: `register_oauth_routes` with no `gate` → `TrustedUpstreamGate` → `requires_identity()` False → direct code issue, and no callback route registered).

- [ ] **Step 7: Commit**

```bash
git add src/pep_oracle/oauth.py tests/test_oauth.py
git commit -m "feat(oauth): gate-aware /oauth/authorize + Cognito callback route"
```

---

## Task 6: Wire the gate into `mount_mcp_if_configured` + relax the guard for Cognito

Select the gate from config at mount time, pass it to `register_oauth_routes`, and adjust the mount guard: `cognito` is the in-app auth (so it does **not** require `PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH=1`), while `trusted_upstream` still requires that flag (unchanged OptiPlex safety).

**Files:**
- Modify: `src/pep_oracle/server.py` (`mount_mcp_if_configured`, lines ~177-205; add `authorize_gate` import)
- Test: `tests/test_server.py` (update `fake_register` signature; add gate tests)

- [ ] **Step 1: Write the failing tests**

In `tests/test_server.py`, update the existing `test_mount_builds_oauth_store_from_config` so its `fake_register` accepts the new `gate` argument (change its signature only):

```python
    def fake_register(app, signing_key, public_url, store, gate=None):
        captured["store"] = store
        captured["gate"] = gate
        raise _Stop  # short-circuit before the (heavy) MCP mount that follows
```

Then append these new tests to `tests/test_server.py`:

```python
def test_mount_trusted_upstream_requires_flag(tmp_path, monkeypatch):
    """Default gate (trusted_upstream): mount refuses without TRUSTS_UPSTREAM_AUTH=1."""
    from fastapi import FastAPI

    from pep_oracle import config, server

    monkeypatch.setenv("PEP_ORACLE_PUBLIC_URL", "https://pep-oracle.example")
    monkeypatch.delenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", raising=False)
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "trusted_upstream")
    monkeypatch.setattr(server, "_resolve_signing_key", lambda: "k")
    assert server.mount_mcp_if_configured(FastAPI()) is False


def test_mount_cognito_gate_skips_upstream_flag(tmp_path, monkeypatch):
    """Cognito gate IS the auth, so mount proceeds without TRUSTS_UPSTREAM_AUTH and
    passes a CognitoGate to register_oauth_routes."""
    from fastapi import FastAPI

    from pep_oracle import authorize_gate, config, server

    captured = {}

    class _Stop(Exception):
        pass

    def fake_register(app, signing_key, public_url, store, gate=None):
        captured["gate"] = gate
        raise _Stop

    monkeypatch.setenv("PEP_ORACLE_PUBLIC_URL", "https://pep-oracle.example")
    monkeypatch.delenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OAUTH_STORE", "sqlite")
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "cognito")
    monkeypatch.setattr(config, "COGNITO_DOMAIN", "https://d.example")
    monkeypatch.setattr(config, "COGNITO_CLIENT_ID", "cid")
    monkeypatch.setattr(config, "COGNITO_CLIENT_SECRET", "sec")
    monkeypatch.setattr(config, "COGNITO_USER_POOL_ID", "ap-southeast-2_pool")
    monkeypatch.setattr(config, "COGNITO_ALLOWED_EMAILS", "me@example.com")
    monkeypatch.setattr(server.oauth, "register_oauth_routes", fake_register)
    monkeypatch.setattr(server, "_resolve_signing_key", lambda: "k")

    try:
        server.mount_mcp_if_configured(FastAPI())
    except _Stop:
        pass
    assert isinstance(captured["gate"], authorize_gate.CognitoGate)


def test_mount_cognito_misconfigured_refuses(tmp_path, monkeypatch):
    """AUTHORIZE_GATE=cognito with missing config must refuse to mount (fail-closed)."""
    from fastapi import FastAPI

    from pep_oracle import config, server

    monkeypatch.setenv("PEP_ORACLE_PUBLIC_URL", "https://pep-oracle.example")
    monkeypatch.delenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", raising=False)
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "cognito")
    monkeypatch.setattr(config, "COGNITO_DOMAIN", "")
    monkeypatch.setattr(config, "COGNITO_CLIENT_ID", "")
    monkeypatch.setattr(config, "COGNITO_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "COGNITO_USER_POOL_ID", "")
    monkeypatch.setattr(config, "COGNITO_ALLOWED_EMAILS", "")
    monkeypatch.setattr(server, "_resolve_signing_key", lambda: "k")
    assert server.mount_mcp_if_configured(FastAPI()) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_server.py -k mount -q`
Expected: FAIL — `mount_mcp_if_configured` doesn't yet branch on the gate; `test_mount_cognito_gate_skips_upstream_flag` fails (it still returns False on the missing flag), and the updated `fake_register` captures no `gate`.

- [ ] **Step 3: Add the import**

In `src/pep_oracle/server.py`, extend the package import on line 17 to include `authorize_gate`:

```python
from pep_oracle import authorize_gate, config as _config, corpus as _corpus, oauth
```

- [ ] **Step 4: Branch the mount guard on the gate**

In `src/pep_oracle/server.py`, replace the guard block in `mount_mcp_if_configured` — the current lines:

```python
    if os.environ.get("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", "") != "1":
        logger.error(
            "PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH != '1' — refusing to mount /mcp. "
            "/oauth/authorize has no app-layer auth and MUST sit behind an upstream "
            "authenticator (e.g. Cloudflare Access on /oauth/authorize). See the "
            "Cloudflare Access setup section in /home/gregg/.claude/plans/mcp-oauth-dcr.md. "
            "Set the var to '1' once that upstream guard is in place."
        )
        return False

    signing_key = _resolve_signing_key()
    from pep_oracle import oauth_store

    store = oauth_store.get_store()
    oauth.register_oauth_routes(app, signing_key, public_url, store)
```

…with:

```python
    if _config.AUTHORIZE_GATE == "cognito":
        # The in-app Cognito identity check IS the authorize-endpoint auth, so the
        # upstream-trust flag isn't required here. Refuse if misconfigured (fail-closed).
        try:
            gate = authorize_gate.get_gate()
        except ValueError as e:
            logger.error("AUTHORIZE_GATE=cognito but misconfigured (%s) — refusing to mount /mcp.", e)
            return False
    else:
        if os.environ.get("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", "") != "1":
            logger.error(
                "PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH != '1' — refusing to mount /mcp. "
                "/oauth/authorize has no app-layer auth and MUST sit behind an upstream "
                "authenticator (e.g. Cloudflare Access on /oauth/authorize), or set "
                "PEP_ORACLE_AUTHORIZE_GATE=cognito for the in-app identity check. See the "
                "Cloudflare Access setup section in /home/gregg/.claude/plans/mcp-oauth-dcr.md. "
                "Set the var to '1' once that upstream guard is in place."
            )
            return False
        gate = authorize_gate.get_gate()  # TrustedUpstreamGate

    signing_key = _resolve_signing_key()
    from pep_oracle import oauth_store

    store = oauth_store.get_store()
    oauth.register_oauth_routes(app, signing_key, public_url, store, gate)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_server.py -q`
Expected: PASS — including the updated `test_mount_builds_oauth_store_from_config` and the three new mount tests.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (all tests; the live-marked ones stay deselected).

- [ ] **Step 7: Commit**

```bash
git add src/pep_oracle/server.py tests/test_server.py
git commit -m "feat(server): select authorize gate at mount; Cognito skips the upstream-trust flag"
```

---

## Task 7: Docs — runbook, CLAUDE.md, `.env.example`

Document the two seams and how an operator stands up the real AWS resources (SSM param + one-user Cognito pool) for a local-against-real smoke. CLAUDE.md is 128 lines — well under the 300 ceiling — so additions are small and no compaction is needed.

**Files:**
- Create: `docs/aws/phase2b2-signing-and-cognito.md`
- Modify: `CLAUDE.md` (env section + the MCP/oauth design bullets)
- Modify: `.env.example` (new vars, commented)

- [ ] **Step 1: Write the runbook**

Create `docs/aws/phase2b2-signing-and-cognito.md`:

```markdown
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
A missing required Cognito var makes mount refuse (fail-closed).

## Flow (cognito)

`/oauth/authorize` validates the MCP request, then 302s the browser to the Cognito
Hosted UI carrying a short-lived HS256 "login-state" JWT (the original MCP params).
After login, Cognito redirects to `/oauth/authorize/callback`, which exchanges the
code, verifies the ID token (RS256 via the pool JWKS) and the email allow-list, then
issues the pep-oracle auth code and redirects back to the MCP client. Stateless: no
session cookie, no extra store rows. The authorize flow is rare (client setup), so the
round-trip never touches query latency.

## Smoke

```bash
# trusted_upstream (default) — unchanged
uv run pytest -q

# against real AWS: export the env above, then
uv run pep-oracle-server   # check logs say "OAuth provider routes registered" and "MCP mounted at /mcp"
```
```

- [ ] **Step 2: Update `.env.example`**

Append to `.env.example`:

```bash

# --- OAuth signing-key backend (Phase 2b2) ---
# local (default): env/file/generate. ssm: HS256 SecureString from SSM.
# PEP_ORACLE_OAUTH_SIGNING_BACKEND=local
# PEP_ORACLE_OAUTH_SIGNING_SSM_PARAM=/pep-oracle/oauth-signing-key
# PEP_ORACLE_OAUTH_SIGNING_SSM_REGION=ap-southeast-2

# --- /oauth/authorize identity gate (Phase 2b2) ---
# trusted_upstream (default): auto-approve behind an upstream authenticator.
# cognito: in-app one-user Cognito identity check (no TRUSTS_UPSTREAM_AUTH needed).
# PEP_ORACLE_AUTHORIZE_GATE=trusted_upstream
# PEP_ORACLE_COGNITO_DOMAIN=https://pep-oracle.auth.ap-southeast-2.amazoncognito.com
# PEP_ORACLE_COGNITO_CLIENT_ID=
# PEP_ORACLE_COGNITO_CLIENT_SECRET=
# PEP_ORACLE_COGNITO_USER_POOL_ID=ap-southeast-2_abc123
# PEP_ORACLE_COGNITO_REGION=ap-southeast-2
# PEP_ORACLE_COGNITO_ALLOWED_EMAILS=you@example.com
```

- [ ] **Step 3: Update `CLAUDE.md`**

In the `## Environment` → `Optional:` list, after the `PEP_ORACLE_OAUTH_SIGNING_KEY` bullet, add:

```markdown
- `PEP_ORACLE_OAUTH_SIGNING_BACKEND` — `local` (default: env/file/generate) or `ssm` (HS256 SecureString from `PEP_ORACLE_OAUTH_SIGNING_SSM_PARAM`, region `PEP_ORACLE_OAUTH_SIGNING_SSM_REGION`). Lambda uses `ssm`; missing/empty param fails closed.
- `PEP_ORACLE_AUTHORIZE_GATE` — `trusted_upstream` (default) or `cognito`. `cognito` enables the in-app one-user Cognito identity gate on `/oauth/authorize` and does NOT require `TRUSTS_UPSTREAM_AUTH=1`; needs `PEP_ORACLE_COGNITO_{DOMAIN,CLIENT_ID,CLIENT_SECRET,USER_POOL_ID,REGION,ALLOWED_EMAILS}` (see `docs/aws/phase2b2-signing-and-cognito.md`).
```

In the MCP-server design bullet about `oauth.py`, update the signing/gate description: the access-token signing key now comes from the pluggable `signing.py` (`local`/`ssm`), and `/oauth/authorize` is gated by a pluggable `authorize_gate.py` (`TrustedUpstreamGate` default; `CognitoGate` brokers a Hosted-UI login → callback verifies the ID token via the pool JWKS + email allow-list, carrying MCP params in a stateless login-state JWT). Replace the trailing claim in the `/oauth/authorize is gated at the edge` bullet to note the new in-app alternative: "…or set `PEP_ORACLE_AUTHORIZE_GATE=cognito` for an in-app identity gate that removes the external-edge dependency (Phase 2b2)."

Keep CLAUDE.md under 300 lines (run `wc -l CLAUDE.md` — expect ~135).

- [ ] **Step 4: Verify tests + CLAUDE.md size**

Run: `uv run pytest -q && wc -l CLAUDE.md`
Expected: tests PASS; CLAUDE.md well under 300 lines.

- [ ] **Step 5: Run `/claude-md-improver` and stage**

Run `/claude-md-improver` (required by the commit hook), then:

```bash
git add docs/aws/phase2b2-signing-and-cognito.md .env.example CLAUDE.md
git commit -m "docs(phase2b2): signing backend + Cognito gate runbook, env, CLAUDE.md"
```

---

## Self-Review

**Spec coverage** (against `docs/superpowers/specs/2026-06-02-aws-mcp-migration-design.md` §4.3 and §4.6, the two 2b2 items):

| Spec requirement | Task |
|---|---|
| §4.3 signing seam, Phase-1 HS256 from SSM SecureString | Task 1 |
| §4.3 "same seam local dev already needs for its HS256 dev key" | Task 1 (`local` backend = unchanged dev behavior) |
| §4.6 replace auto-approve with a Cognito identity check (one-user pool) | Tasks 2–6 |
| §4.6 "valid Cognito session before it approves" | Task 5 (authorize → IdP; callback verifies before issuing) |
| §4.6 "removes the fail-open-if-misconfigured risk" | Task 6 (cognito fail-closed on missing config) + Task 3 (email allow-list) |
| §4.6 "authorize flow is rare … latency never touches query path" | stateless login-state JWT, no per-query cost (design) |
| Phase-1 only (KMS asymmetric = Phase 5) | explicitly out of scope here |
| CDK provisioning of SSM param + Cognito pool | deferred to Phase 2c (runbook documents manual setup) |

**Placeholder scan:** every code/step shows complete code; no TBD/"handle errors"/"similar to". ✔

**Type/name consistency:**
- `register_oauth_routes(app, signing_key, public_url, store, gate=None)` — defined Task 5, called Task 6, faked in tests with matching arity. ✔
- `CognitoGate.login_redirect(*, redirect_uri, login_state)` / `exchange_and_verify(*, code, redirect_uri)` / `requires_identity()` — defined Tasks 2/4, called identically in oauth.py (Task 5) and the fake gate. ✔
- `_encode_login_state` / `_decode_login_state` signatures match between oauth.py and the tests. ✔
- `CALLBACK_PATH`, `IdentityError`, `TrustedUpstreamGate`, `AuthorizeGate` imported from `authorize_gate` into `oauth.py`. ✔
- `config.OAUTH_SIGNING_BACKEND/…SSM_PARAM/…SSM_REGION`, `config.AUTHORIZE_GATE`, `config.COGNITO_*` — added Tasks 1/2, read in `signing.py`, `authorize_gate.py`, `server.py`. ✔
- `server._resolve_signing_key` name preserved (still patched by the existing mount test). ✔

**Verified in-env before planning:** moto mocks SSM SecureString; PyJWT `RSAAlgorithm.from_jwk` + RS256 decode round-trips (Task 3 approach is sound).
