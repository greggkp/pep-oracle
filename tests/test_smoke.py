"""Unit tests for the post-deploy smoke checks (scripts/smoke.py), fetchers mocked."""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import smoke  # noqa: E402


def _healthy(url, timeout=10.0):
    if url.endswith("/version"):
        body = json.dumps({"code_git_sha": "abc1234", "code_semver": "v1.0.0",
                           "corpus_version": "v0002"}).encode()
        return 200, body
    return 200, b"{}"


def test_check_passes_when_all_good(monkeypatch):
    monkeypatch.setattr(smoke, "_get", _healthy)
    monkeypatch.setattr(smoke, "_post_no_token", lambda url, timeout=10.0: 401)
    assert smoke.check("https://x", expect_sha="abc1234", expect_semver="v1.0.0") == []


def test_check_flags_stale_version_and_open_mcp(monkeypatch):
    def stale(url, timeout=10.0):
        if url.endswith("/version"):
            return 200, json.dumps({"code_git_sha": "old", "code_semver": "v0.9.0",
                                    "corpus_version": "v0002"}).encode()
        return 200, b"{}"
    monkeypatch.setattr(smoke, "_get", stale)
    monkeypatch.setattr(smoke, "_post_no_token", lambda url, timeout=10.0: 200)  # open = bad
    fails = smoke.check("https://x", expect_sha="abc1234", expect_semver="v1.0.0")
    assert any("code_git_sha" in f for f in fails)
    assert any("code_semver" in f for f in fails)
    assert any("/mcp" in f for f in fails)


def test_check_flags_non_json_version_body(monkeypatch):
    def html(url, timeout=10.0):
        if url.endswith("/version"):
            return 200, b"<html>gateway timeout</html>"
        return 200, b"{}"
    monkeypatch.setattr(smoke, "_get", html)
    monkeypatch.setattr(smoke, "_post_no_token", lambda url, timeout=10.0: 401)
    fails = smoke.check("https://x")
    assert any("non-JSON" in f for f in fails)
