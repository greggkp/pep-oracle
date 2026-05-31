import json
import tarfile

import pep_oracle.backup as backup
from pep_oracle.backup import build_bundle, prune_local, push_bundle, run_backup


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _state(monkeypatch, tmp_path):
    """Point config's state/cache paths at a tmp tree with content."""
    monkeypatch.setattr(backup.config, "SPEAKER_PROFILES_PATH", _write(tmp_path / "speaker_profiles.json", "{}"))
    monkeypatch.setattr(backup.config, "TOPICS_PATH", _write(tmp_path / "topics.json", "[]"))
    tdir = tmp_path / "cache" / "transcripts"
    ddir = tmp_path / "cache" / "diarization"
    _write(tdir / "g1.whisper.json", "{}")
    _write(ddir / "g1.json", "{}")
    monkeypatch.setattr(backup.config, "TRANSCRIPT_CACHE_DIR", tdir)
    monkeypatch.setattr(backup.config, "DIARIZATION_CACHE_DIR", ddir)


def test_build_bundle_includes_export_state_and_caches(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    export_json = _write(tmp_path / "episodes.json", '[{"id": "c1"}]')
    staging = tmp_path / "backup"

    tarball = build_bundle(staging, export_json, timestamp="20260531-120000")

    assert tarball.name == "pep-oracle-backup-20260531-120000.tar.gz"
    with tarfile.open(tarball) as tar:
        names = set(tar.getnames())
    assert "episodes.json" in names
    assert "speaker_profiles.json" in names
    assert "topics.json" in names
    assert "cache/transcripts/g1.whisper.json" in names
    assert "cache/diarization/g1.json" in names


def test_build_bundle_skips_missing_optional_state(monkeypatch, tmp_path):
    # No profiles/topics/caches on disk — only the export must be bundled.
    monkeypatch.setattr(backup.config, "SPEAKER_PROFILES_PATH", tmp_path / "absent_profiles.json")
    monkeypatch.setattr(backup.config, "TOPICS_PATH", tmp_path / "absent_topics.json")
    monkeypatch.setattr(backup.config, "TRANSCRIPT_CACHE_DIR", tmp_path / "absent_t")
    monkeypatch.setattr(backup.config, "DIARIZATION_CACHE_DIR", tmp_path / "absent_d")
    export_json = _write(tmp_path / "episodes.json", "[]")

    tarball = build_bundle(tmp_path / "backup", export_json, timestamp="t")

    with tarfile.open(tarball) as tar:
        assert tar.getnames() == ["episodes.json"]


def test_push_bundle_invokes_rclone_copy(tmp_path):
    tarball = _write(tmp_path / "b.tar.gz")
    calls = []

    def runner(cmd, **kw):
        calls.append((cmd, kw))

    push_bundle(tarball, "b2:pep-oracle-backup", runner=runner)

    cmd, kw = calls[0]
    assert cmd[:2] == ["rclone", "copy"]
    assert str(tarball) in cmd
    assert "b2:pep-oracle-backup" in cmd
    assert kw.get("check") is True


def test_push_bundle_rejects_empty_remote(tmp_path):
    tarball = _write(tmp_path / "b.tar.gz")
    try:
        push_bundle(tarball, "", runner=lambda *a, **k: None)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_prune_local_keeps_newest_n(tmp_path):
    for ts in ["20260101-000000", "20260201-000000", "20260301-000000"]:
        _write(tmp_path / f"pep-oracle-backup-{ts}.tar.gz")
    removed = prune_local(tmp_path, keep=2)

    remaining = sorted(p.name for p in tmp_path.glob("pep-oracle-backup-*.tar.gz"))
    assert remaining == [
        "pep-oracle-backup-20260201-000000.tar.gz",
        "pep-oracle-backup-20260301-000000.tar.gz",
    ]
    assert [p.name for p in removed] == ["pep-oracle-backup-20260101-000000.tar.gz"]


def test_run_backup_exports_bundles_and_pushes(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    monkeypatch.setattr(backup.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr("pep_oracle.store.get_client", lambda: object())
    monkeypatch.setattr("pep_oracle.store.get_collection", lambda client: object())
    monkeypatch.setattr("pep_oracle.store.export_episodes", lambda col: [{"id": "c1", "document": "d", "embedding": [0.1], "metadata": {}}])
    pushed = []

    tarball = run_backup("b2:bak", keep_local=3, runner=lambda cmd, **kw: pushed.append(cmd))

    assert tarball.exists()
    assert pushed and pushed[0][:2] == ["rclone", "copy"]
    # the loose export json is not left lying around — only the tarball
    assert not (tmp_path / "backup" / "episodes.json").exists()
    with tarfile.open(tarball) as tar:
        data = json.loads(tar.extractfile("episodes.json").read())
    assert data[0]["id"] == "c1"
