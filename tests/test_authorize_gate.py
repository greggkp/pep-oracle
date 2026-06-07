"""Tests for the /oauth/authorize identity gate (authorize_gate.py)."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

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


def test_get_gate_unknown_raises(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "cogito-typo")
    with pytest.raises(ValueError):
        authorize_gate.get_gate()


def test_cognito_from_config_whitespace_emails_raises(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "cognito")
    monkeypatch.setattr(config, "COGNITO_DOMAIN", "https://d.example")
    monkeypatch.setattr(config, "COGNITO_CLIENT_ID", "cid")
    monkeypatch.setattr(config, "COGNITO_CLIENT_SECRET", "sec")
    monkeypatch.setattr(config, "COGNITO_USER_POOL_ID", "ap-southeast-2_pool")
    monkeypatch.setattr(config, "COGNITO_ALLOWED_EMAILS", "  ,  ")
    with pytest.raises(ValueError):
        authorize_gate.get_gate()


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
    assert "sub" in claims  # returns the full verified claims, not just email


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


def test_verify_id_token_jwks_fetch_error_raises(monkeypatch):
    priv_pem, _ = _rsa_keypair()
    gate = _gate()

    def boom():
        raise authorize_gate.requests.RequestException("connreset")

    monkeypatch.setattr(gate, "_fetch_jwks", boom)
    tok = _id_token(priv_pem, iss=gate.issuer, aud=gate.client_id, email="me@example.com")
    with pytest.raises(authorize_gate.IdentityError):
        gate._verify_id_token(tok)


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
    assert captured["data"]["client_id"] == gate.client_id
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
