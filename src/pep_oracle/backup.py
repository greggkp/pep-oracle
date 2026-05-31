"""Backup bundle creation + off-site push via rclone.

The single recompute-free restore artifact is the export JSON (chunks +
embeddings + metadata for every ingested episode); restoring it with
`pep-oracle import` needs no Modal, no re-embed, and no LLM spend. We bundle it
with the small derived state (speaker profiles, topics) and the Modal output
caches (transcripts + diarization). The caches are re-ingest insurance — they
let a from-scratch re-ingest skip Modal for cached episodes — not the primary
restore path, since they may be incomplete relative to what's ingested.

The bundle is one timestamped tarball pushed to an rclone remote, so losing the
disk costs nothing to recover. See deploy/restore.md for the restore steps.
"""

import json
import subprocess
import tarfile
import time
from pathlib import Path

from pep_oracle import config


def _add_if_exists(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    if path.exists():
        tar.add(path, arcname=arcname)


def build_bundle(staging_dir: Path, export_json: Path, *, timestamp: str | None = None) -> Path:
    """Assemble the backup tarball from an already-written export JSON plus the
    derived state and Modal caches. Returns the tarball path."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    ts = timestamp or time.strftime("%Y%m%d-%H%M%S")
    tarball = staging_dir / f"pep-oracle-backup-{ts}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(export_json, arcname="episodes.json")
        _add_if_exists(tar, config.SPEAKER_PROFILES_PATH, "speaker_profiles.json")
        _add_if_exists(tar, config.TOPICS_PATH, "topics.json")
        _add_if_exists(tar, config.TRANSCRIPT_CACHE_DIR, "cache/transcripts")
        _add_if_exists(tar, config.DIARIZATION_CACHE_DIR, "cache/diarization")
    return tarball


def push_bundle(tarball: Path, remote: str, *, runner=subprocess.run) -> None:
    """Copy the tarball to an rclone remote (e.g. ``b2:pep-oracle-backup``)."""
    if not remote:
        raise ValueError("No rclone remote configured (set PEP_ORACLE_BACKUP_REMOTE).")
    runner(["rclone", "copy", str(tarball), remote], check=True)


def prune_local(staging_dir: Path, keep: int = 3) -> list[Path]:
    """Delete all but the newest ``keep`` local backup tarballs (timestamped
    names sort chronologically). Returns the removed paths."""
    tarballs = sorted(staging_dir.glob("pep-oracle-backup-*.tar.gz"))
    removed = tarballs[:-keep] if keep > 0 else []
    for p in removed:
        p.unlink()
    return removed


def run_backup(remote: str, *, keep_local: int = 3, runner=subprocess.run) -> Path:
    """Export the corpus, bundle it with derived state + caches, push to the
    rclone remote, and prune old local tarballs. Returns the tarball path."""
    from pep_oracle.store import export_episodes, get_client, get_collection

    staging = config.DATA_DIR / "backup"
    staging.mkdir(parents=True, exist_ok=True)

    items = export_episodes(get_collection(get_client()))
    export_json = staging / "episodes.json"
    export_json.write_text(json.dumps(items))

    tarball = build_bundle(staging, export_json)
    export_json.unlink()  # keep only the self-contained tarball
    push_bundle(tarball, remote, runner=runner)
    prune_local(staging, keep_local)
    return tarball
