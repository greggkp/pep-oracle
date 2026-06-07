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
