"""One-time corpus backfill: re-embed the existing `pep-oracle export` JSON with
the Bedrock backend and publish v0001 of the corpus artifact.

Transcription/diarization are NOT re-run — the export already holds chunk text +
metadata; only the embedding vectors are recomputed (old bge-large vectors are
discarded). One Bedrock pass over ~10k short texts, a few cents.
"""

from __future__ import annotations

import json
import subprocess

from pep_oracle import config, corpus
from pep_oracle.embeddings import embed_texts


def _current_git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — provenance only; never block a backfill
        return "unknown"


def backfill(
    *,
    export_path: str,
    dest: str,
    version: str = "v0001",
    embed=embed_texts,
    embed_model: str | None = None,
    dims: int | None = None,
    git_sha: str | None = None,
    built_at: str | None = None,
) -> corpus.Manifest:
    """Read export JSON, re-embed each chunk's text, publish <dest>/corpus/<version>.*."""
    with open(export_path) as f:
        items = json.load(f)

    texts = [it["document"] for it in items]
    vectors = embed(texts)
    rows = [
        {
            "chunk_id": it["id"],
            "text": it["document"],
            "embedding": vec,
            "metadata": it["metadata"],
        }
        for it, vec in zip(items, vectors)
    ]

    if built_at is None:
        from datetime import datetime, timezone

        built_at = datetime.now(timezone.utc).isoformat()

    return corpus.write_artifact(
        rows,
        dest=dest,
        version=version,
        embed_model=embed_model or config.EMBED_MODEL,
        dims=dims or config.EMBED_DIMS,
        git_sha=git_sha if git_sha is not None else _current_git_sha(),
        built_at=built_at,
    )
