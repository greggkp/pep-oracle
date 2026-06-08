"""GET /version: the release tag (PEP_ORACLE_SEMVER) overrides the package version."""
from __future__ import annotations

from pep_oracle import config as _config
from pep_oracle import server


def test_code_version_prefers_semver_env(monkeypatch):
    monkeypatch.setattr(_config, "SEMVER", "v1.2.3")
    monkeypatch.setattr(_config, "GIT_SHA", "abc1234")
    semver, sha = server._code_version()
    assert semver == "v1.2.3"
    assert sha == "abc1234"


def test_code_version_falls_back_to_package_version(monkeypatch):
    monkeypatch.setattr(_config, "SEMVER", "")
    monkeypatch.setattr(_config, "GIT_SHA", "abc1234")
    semver, _ = server._code_version()
    assert semver == "0.1.0"  # pyproject [project] version
