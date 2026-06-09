"""Tests for the FastAPI server's product surface (no GUI): /health, /version,
the MCP mount gating, the bearer wrapper, and the Lambda handler."""

import pytest
from fastapi.testclient import TestClient

from pep_oracle import server


def test_health_ok():
    client = TestClient(server.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_version_reports_code(tmp_path, monkeypatch):
    monkeypatch.setattr(server._config, "SEMVER", "v9.9.9")
    monkeypatch.setattr(server._config, "GIT_SHA", "abc1234")
    # Point CORPUS_URI at an empty dir so /version hits the corpus_error branch
    # deterministically (independent of any local ~/.pep-oracle corpus).
    monkeypatch.setattr(server._config, "CORPUS_URI", str(tmp_path))
    client = TestClient(server.app)
    body = client.get("/version").json()
    assert body["code_semver"] == "v9.9.9"
    assert body["code_git_sha"] == "abc1234"
    assert "corpus_version" not in body


def test_version_reports_corpus_when_manifest_loads(tmp_path, monkeypatch):
    from pep_oracle import corpus

    corpus.write_artifact(
        [{"chunk_id": "a", "text": "x", "embedding": [1.0, 0.0],
          "metadata": {"episode_number": 251, "episode_date": "2026-04-01",
                       "episode_guid": "g", "episode_title": "t",
                       "start_time": 0.0, "end_time": 1.0}}],
        dest=str(tmp_path), version="v0042",
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s",
        built_at="2026-06-01T06:14:00+00:00",
    )
    monkeypatch.setattr(server._config, "CORPUS_URI", str(tmp_path))
    client = TestClient(server.app)
    body = client.get("/version").json()
    assert body["corpus_version"] == "v0042"
    assert body["corpus_episode_range"] == [251, 251]
    assert body["embed_model"] == "amazon.titan-embed-text-v2:0"
    assert body["corpus_built_at"] == "2026-06-01T06:14:00+00:00"


def test_version_corpus_error_is_generic_and_leaks_no_path(tmp_path, monkeypatch):
    """/version is public (not behind the /mcp bearer gate), so a corpus load failure
    must return a generic marker, not the raw exception (which would leak the corpus
    path / S3 bucket)."""
    secret_path = str(tmp_path / "nonexistent-secret-bucket-name")
    monkeypatch.setattr(server._config, "CORPUS_URI", secret_path)  # no current.json -> load fails
    client = TestClient(server.app)
    r = client.get("/version")
    assert r.status_code == 200  # never 500s
    assert r.json()["corpus_error"] == "corpus manifest unavailable"
    assert "nonexistent-secret-bucket-name" not in r.text  # internal path not leaked


# --- MCP mount gating -------------------------------------------------------


def test_mcp_mount_skipped_without_public_url(monkeypatch):
    monkeypatch.delenv("PEP_ORACLE_PUBLIC_URL", raising=False)
    from fastapi import FastAPI

    app = FastAPI()
    assert server.mount_mcp_if_configured(app) is False


def test_mount_builds_oauth_store_from_config(tmp_path, monkeypatch):
    """mount_mcp_if_configured builds an OAuthStore from config and passes the
    STORE OBJECT (not a db-path string) to register_oauth_routes."""
    from fastapi import FastAPI

    from pep_oracle import authorize_gate, config, oauth_store

    captured = {}

    class _Stop(Exception):
        pass

    def fake_register(app, signing_key, public_url, store, gate=None):
        captured["store"] = store
        captured["gate"] = gate
        raise _Stop  # short-circuit before the (heavy) MCP mount that follows

    monkeypatch.setenv("PEP_ORACLE_PUBLIC_URL", "https://pep-oracle.example")
    monkeypatch.setenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)          # don't touch ~/.pep-oracle
    monkeypatch.setattr(config, "OAUTH_STORE", "sqlite")
    monkeypatch.setattr(server.oauth, "register_oauth_routes", fake_register)
    monkeypatch.setattr(server, "_resolve_signing_key", lambda: "k")

    try:
        server.mount_mcp_if_configured(FastAPI())
    except _Stop:
        pass

    store = captured["store"]
    # a store object, NOT a path string:
    assert not isinstance(store, str)
    assert hasattr(store, "get_refresh") and hasattr(store, "revoke_refresh")
    assert isinstance(store, oauth_store.SqliteStore)
    # default path resolves the trusted_upstream gate
    assert isinstance(captured["gate"], authorize_gate.TrustedUpstreamGate)


def test_mount_trusted_upstream_requires_flag(tmp_path, monkeypatch):
    """Default gate (trusted_upstream): mount refuses without TRUSTS_UPSTREAM_AUTH=1."""
    from fastapi import FastAPI

    from pep_oracle import config

    monkeypatch.setenv("PEP_ORACLE_PUBLIC_URL", "https://pep-oracle.example")
    monkeypatch.delenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", raising=False)
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "trusted_upstream")
    monkeypatch.setattr(server, "_resolve_signing_key", lambda: "k")
    assert server.mount_mcp_if_configured(FastAPI()) is False


def test_mount_cognito_gate_skips_upstream_flag(tmp_path, monkeypatch):
    """Cognito gate IS the auth, so mount proceeds without TRUSTS_UPSTREAM_AUTH and
    passes a CognitoGate to register_oauth_routes."""
    from fastapi import FastAPI

    from pep_oracle import authorize_gate, config

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

    from pep_oracle import config

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


def test_mount_unknown_gate_refuses(tmp_path, monkeypatch):
    """An unrecognized AUTHORIZE_GATE value must refuse to mount (fail-closed),
    even with TRUSTS_UPSTREAM_AUTH=1 — the mount's else branch refuses unknown
    gate values (fail-closed) before get_gate() is ever called."""
    from fastapi import FastAPI

    from pep_oracle import config

    monkeypatch.setenv("PEP_ORACLE_PUBLIC_URL", "https://pep-oracle.example")
    monkeypatch.setenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", "1")
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "bogus-typo")
    monkeypatch.setattr(server, "_resolve_signing_key", lambda: "k")
    assert server.mount_mcp_if_configured(FastAPI()) is False


# --- Lambda handler + warm-reinvocation -------------------------------------


def _apigw_v2_event(method="GET", path="/health"):
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"host": "test.example", "content-length": "0"},
        "requestContext": {
            "domainName": "test.example",
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1",
                     "sourceIp": "1.2.3.4"},
            "stage": "$default",
            "requestId": "req-1",
        },
        "isBase64Encoded": False,
    }


def test_lambda_handler_is_constructed():
    """server.handler is a Mangum ASGI adapter wrapping the FastAPI app, so the
    same app runs under uvicorn locally and Lambda in prod."""
    assert server.handler is not None
    assert server.handler.__class__.__name__ == "Mangum"


def test_mcp_mount_survives_warm_mangum_reinvocation(monkeypatch, tmp_path):
    """Mangum runs the ASGI lifespan per invocation. The MCP mount must not break on
    the 2nd (warm) invocation — i.e. mounting must NOT drive the once-per-instance
    StreamableHTTPSessionManager.run() from the per-invoke lifespan."""
    from fastapi import FastAPI

    from pep_oracle import config

    mangum = pytest.importorskip("mangum")

    monkeypatch.setenv("PEP_ORACLE_PUBLIC_URL", "https://test.example")
    monkeypatch.setenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", "1")
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "trusted_upstream")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OAUTH_STORE", "sqlite")
    monkeypatch.setattr(server, "_resolve_signing_key", lambda: "k" * 40)

    app = FastAPI()

    @app.get("/health")
    async def _health():
        return {"status": "ok"}

    assert server.mount_mcp_if_configured(app) is True

    handler = mangum.Mangum(app)
    event = _apigw_v2_event()
    r1 = handler(event, None)
    assert r1["statusCode"] == 200, r1
    # 2nd warm invocation in the same process — the bug surfaced here (LifespanFailure
    # from StreamableHTTPSessionManager.run() called twice on the singleton).
    r2 = handler(event, None)
    assert r2["statusCode"] == 200, r2


def test_mcp_host_check_disabled_and_slash_normalized(monkeypatch, tmp_path):
    """Behind CloudFront→APIGW the Lambda sees the proxy Host, so the MCP DNS-rebinding
    host-check must be off (else 421), and /mcp (no slash) must be served directly via the
    Lambda handler's normalizer (else a cross-host 307 that drops Authorization)."""
    from fastapi import FastAPI

    from pep_oracle import config, mcp_server

    mangum = pytest.importorskip("mangum")

    monkeypatch.setenv("PEP_ORACLE_PUBLIC_URL", "https://test.example")
    monkeypatch.setenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", "1")
    monkeypatch.setattr(config, "AUTHORIZE_GATE", "trusted_upstream")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "OAUTH_STORE", "sqlite")
    monkeypatch.setattr(server, "_resolve_signing_key", lambda: "k" * 40)

    app = FastAPI()
    assert server.mount_mcp_if_configured(app) is True

    # host/origin DNS-rebinding check disabled (the Lambda can't see the public Host)
    assert mcp_server.mcp.settings.transport_security.enable_dns_rebinding_protection is False

    # /mcp (no trailing slash) reaches the bearer wrapper (401 no-token) — NOT a 307 redirect
    handler = mangum.Mangum(server._McpSlashNormalizer(app))
    r = handler(_apigw_v2_event(method="POST", path="/mcp"), None)
    assert r["statusCode"] == 401, r
