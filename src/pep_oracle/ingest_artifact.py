"""Artifact-native incremental ingest (Phase 3, AWS-native).

Load the current corpus artifact, diff its episode GUIDs against the RSS feed,
transcribe/diarize/chunk/Bedrock-embed ONLY new episodes, merge rows, and publish
a new corpus version (vN+1) to S3 — no ChromaDB. Idempotent: a failed run publishes
nothing (the current.json flip is atomic + last), so the next run retries.
"""

from __future__ import annotations

import datetime as _dt
import logging
import tempfile
from pathlib import Path

from pep_oracle import _storage as storage
from pep_oracle import config
from pep_oracle.corpus import load_current, write_artifact
from pep_oracle.feed import fetch_episodes
from pep_oracle.ingest import episode_chunks_and_embeddings
from pep_oracle.store import _chunk_metadata

logger = logging.getLogger(__name__)


def _next_version(current: str | None) -> str:
    n = int(current[1:]) + 1 if current and current.startswith("v") else 1
    return f"v{n:04d}"


def _download_profiles(dest: str) -> Path | None:
    """Download speaker_profiles.json (config.SPEAKER_PROFILES_URI) to a temp file
    and return its path, or None if absent. Passed to diarization as profile_path."""
    uri = config.SPEAKER_PROFILES_URI
    try:
        data = storage.get_bytes(uri)
    except Exception:  # noqa: BLE001 — absent refs → generic-label fallback, don't fail ingest
        logger.warning("speaker profiles not found at %s; diarization uses fallback labels", uri)
        return None
    path = Path(tempfile.gettempdir()) / "speaker_profiles.json"
    path.write_bytes(data)
    return path


def _corpus_rows(corpus) -> list[dict]:
    return [
        {"chunk_id": cid, "text": doc, "embedding": emb, "metadata": meta}
        for cid, doc, emb, meta in zip(corpus.ids, corpus.docs, corpus.embeddings, corpus.metas)
    ]


def ingest_artifact_incremental(
    *,
    dest: str | None = None,
    diarize: bool = True,
    git_sha: str = "",
    now_iso: str | None = None,
):
    """Publish a new corpus version with any episodes from the feed not already in
    the current artifact. Returns the new Manifest, or None if there were none."""
    if config.EMBED_BACKEND != "bedrock":
        raise RuntimeError("ingest_artifact requires PEP_ORACLE_EMBED_BACKEND=bedrock")
    dest = dest or config.CORPUS_URI

    corpus = load_current(dest)
    existing_guids = {m["episode_guid"] for m in corpus.metas}
    new_eps = [e for e in fetch_episodes() if e.guid not in existing_guids]
    if not new_eps:
        logger.info("ingest_artifact: no new episodes; current=%s", corpus.version)
        return None
    logger.info("ingest_artifact: %d new episode(s) on top of %s", len(new_eps), corpus.version)

    profile_path = _download_profiles(dest)
    rows = _corpus_rows(corpus)
    for ep in sorted(new_eps, key=lambda e: e.episode_number or 0):
        chunks, embeddings = episode_chunks_and_embeddings(
            ep, diarize=diarize, profile_path=profile_path
        )
        for chunk, emb in zip(chunks, embeddings):
            rows.append(
                {"chunk_id": chunk.chunk_id, "text": chunk.text,
                 "embedding": emb, "metadata": _chunk_metadata(chunk)}
            )

    version = _next_version(corpus.version)
    built_at = now_iso or _dt.datetime.now(_dt.timezone.utc).isoformat()
    # Single-run assumption: daily cadence won't self-overlap; the write-then-flip
    # below is last-writer-wins, so avoid concurrent manual runs (see runbook).
    manifest = write_artifact(
        rows, dest=dest, version=version, embed_model=config.EMBED_MODEL,
        dims=config.EMBED_DIMS, git_sha=git_sha or getattr(config, "GIT_SHA", ""), built_at=built_at,
    )
    logger.info("ingest_artifact: published %s (%d chunks, eps %s)",
                version, manifest.chunk_count, manifest.episode_range)
    return manifest
