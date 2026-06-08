"""Tests for CLI commands (src/pep_oracle/cli.py)."""

from __future__ import annotations


def test_ingest_artifact_command_invokes_orchestrator(monkeypatch):
    from click.testing import CliRunner
    from pep_oracle import cli as cli_mod

    called = {}

    def fake(**kwargs):
        called.update(kwargs)

        class _M:
            chunk_count = 3
            episode_range = [169, 300]

        return _M()

    monkeypatch.setattr("pep_oracle.ingest_artifact.ingest_artifact_incremental", fake)

    r = CliRunner().invoke(cli_mod.cli, ["ingest-artifact", "--dest", "s3://b"])
    assert r.exit_code == 0, r.output
    assert called["dest"] == "s3://b"
    assert called["diarize"] is True
