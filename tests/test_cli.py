"""Tests for CLI commands (src/pep_oracle/cli.py)."""

from __future__ import annotations

from click.testing import CliRunner

from pep_oracle.cli import cli


def test_help_lists_only_surviving_commands():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for name in ("ingest-artifact", "eval-retrieval"):
        assert name in result.output
    for gone in ("episodes", "ask", "status", "export", "import", "backup"):
        assert gone not in result.output


def test_ingest_artifact_has_help():
    result = CliRunner().invoke(cli, ["ingest-artifact", "--help"])
    assert result.exit_code == 0


def test_ingest_artifact_command_invokes_orchestrator(monkeypatch):
    called = {}

    def fake(**kwargs):
        called.update(kwargs)

        class _M:
            chunk_count = 3
            episode_range = [169, 300]

        return _M()

    monkeypatch.setattr("pep_oracle.ingest_artifact.ingest_artifact_incremental", fake)

    r = CliRunner().invoke(cli, ["ingest-artifact", "--dest", "s3://b"])
    assert r.exit_code == 0, r.output
    assert called["dest"] == "s3://b"
    assert called["diarize"] is True
