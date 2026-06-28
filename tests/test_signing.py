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
        with pytest.raises(Exception):  # noqa: B017
            signing.resolve_signing_key()


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setattr(config, "OAUTH_SIGNING_BACKEND", "kms-not-yet")
    with pytest.raises(ValueError):
        signing.resolve_signing_key()
