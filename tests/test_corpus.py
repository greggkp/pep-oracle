import hashlib
import json

import pep_oracle.corpus as corpus


def _row(cid, text, ep, embedding):
    return {
        "chunk_id": cid,
        "text": text,
        "embedding": embedding,
        "metadata": {
            "episode_guid": f"g{ep}",
            "episode_title": f"Ep {ep}",
            "episode_date": "2026-01-01",
            "episode_number": ep,
            "start_time": 0.0,
            "end_time": 10.0,
        },
    }


def test_write_artifact_emits_parquet_manifest_and_current(tmp_path):
    rows = [
        _row("a", "byrd rule reconciliation", 251, [0.1, 0.2]),
        _row("b", "tariffs section 122", 253, [0.3, 0.4]),
    ]
    manifest = corpus.write_artifact(
        rows,
        dest=str(tmp_path),
        version="v0001",
        embed_model="amazon.titan-embed-text-v2:0",
        dims=2,
        git_sha="abc1234",
        built_at="2026-06-02T00:00:00+00:00",
    )

    base = tmp_path / "corpus"
    assert (base / "v0001.parquet").exists()
    assert (base / "v0001.manifest.json").exists()
    assert (base / "current.json").exists()

    # Manifest content
    assert manifest.chunk_count == 2
    assert manifest.episode_range == [251, 253]
    assert manifest.embed_model == "amazon.titan-embed-text-v2:0"
    assert manifest.dims == 2

    # current.json points at the version and matches the parquet sha256
    cur = json.loads((base / "current.json").read_text())
    assert cur["version"] == "v0001"
    parquet_sha = hashlib.sha256((base / "v0001.parquet").read_bytes()).hexdigest()
    assert cur["sha256"] == parquet_sha == manifest.sha256


def test_write_artifact_handles_missing_episode_numbers(tmp_path):
    rows = [_row("a", "x", 0, [0.1, 0.2])]  # 0 == store sentinel for "no episode"
    rows[0]["metadata"]["episode_number"] = 0
    manifest = corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="m", dims=2, git_sha="s", built_at="t",
    )
    assert manifest.episode_range == [None, None]
