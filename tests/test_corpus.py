import hashlib
import json

import pep_oracle.corpus as corpus
import pep_oracle.hybrid as _hybrid
from pep_oracle import config as _config
from pep_oracle.hybrid import hybrid_search


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


def test_inmemory_corpus_roundtrip_and_get_shape(tmp_path):
    rows = [
        _row("a", "byrd rule reconciliation", 251, [1.0, 0.0]),
        _row("b", "weather and chit chat", 252, [0.0, 1.0]),
    ]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="m", dims=2, git_sha="s", built_at="t",
    )

    c = corpus.load_current(str(tmp_path))
    assert c.count() == 2
    assert c.name == "pep_oracle"

    got = c.get(include=["documents", "embeddings", "metadatas"])
    assert got["ids"] == ["a", "b"]
    assert got["documents"][0] == "byrd rule reconciliation"
    assert list(got["embeddings"][0]) == [1.0, 0.0]
    assert got["metadatas"][0]["episode_number"] == 251
    # include is honored: omit a key -> absent
    assert "documents" not in c.get(include=["metadatas"])


def test_inmemory_corpus_is_drop_in_for_hybrid_search(tmp_path):
    _hybrid._CACHE.clear()
    rows = [
        _row("a", "byrd rule reconciliation senate", 251, [1.0, 0.0]),
        _row("b", "weather and chit chat", 252, [0.0, 1.0]),
    ]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="m", dims=2, git_sha="s", built_at="t",
    )
    c = corpus.load_current(str(tmp_path))

    results = hybrid_search(c, "byrd rule", [1.0, 0.0], top_k=2)
    assert results[0]["chunk_id"] == "a"
    assert set(results[0]) >= {
        "chunk_id", "text", "distance", "episode_guid",
        "episode_title", "episode_date", "episode_number",
        "start_time", "end_time",
    }


def test_load_current_rejects_corrupt_parquet(tmp_path):
    rows = [_row("a", "x", 251, [1.0, 0.0])]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="m", dims=2, git_sha="s", built_at="t",
    )
    # Corrupt the parquet so its sha256 no longer matches current.json
    (tmp_path / "corpus" / "v0001.parquet").write_bytes(b"corrupted")
    try:
        corpus.load_current(str(tmp_path))
        assert False, "expected a sha256 mismatch error"
    except ValueError as exc:
        assert "sha256" in str(exc).lower()


def test_load_manifest_returns_version_and_manifest(tmp_path):
    rows = [_row("a", "x", 251, [1.0, 0.0]), _row("b", "y", 253, [0.0, 1.0])]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0007",
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s",
        built_at="2026-06-02T00:00:00+00:00",
    )
    version, manifest = corpus.load_manifest(str(tmp_path))
    assert version == "v0007"
    assert manifest.embed_model == "amazon.titan-embed-text-v2:0"
    assert manifest.dims == 2
    assert manifest.episode_range == [251, 253]


def test_validate_serving_passes_when_dims_and_model_match(tmp_path, monkeypatch):
    rows = [_row("a", "x", 251, [1.0, 0.0])]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s", built_at="t",
    )
    monkeypatch.setattr(_config, "EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    c = corpus.load_current(str(tmp_path))
    corpus._validate_serving(c, str(tmp_path))  # no raise


def test_validate_serving_raises_on_embed_model_mismatch(tmp_path, monkeypatch):
    rows = [_row("a", "x", 251, [1.0, 0.0])]
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s", built_at="t",
    )
    monkeypatch.setattr(_config, "EMBED_MODEL", "some-other-model")  # wrong model vs Titan corpus
    c = corpus.load_current(str(tmp_path))
    try:
        corpus._validate_serving(c, str(tmp_path))
        assert False, "expected an embedder-mismatch error"
    except ValueError as exc:
        assert "embed" in str(exc).lower()


def test_validate_serving_raises_on_dims_mismatch(tmp_path, monkeypatch):
    rows = [_row("a", "x", 251, [1.0, 0.0])]  # 2-d vectors
    corpus.write_artifact(
        rows, dest=str(tmp_path), version="v0001",
        embed_model="amazon.titan-embed-text-v2:0", dims=99, git_sha="s", built_at="t",  # manifest lies: 99 != 2
    )
    monkeypatch.setattr(_config, "EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    c = corpus.load_current(str(tmp_path))
    try:
        corpus._validate_serving(c, str(tmp_path))
        assert False, "expected a dims-mismatch error"
    except ValueError as exc:
        assert "dim" in str(exc).lower()


def _publish(tmp_path, version, ep, text):
    corpus.write_artifact(
        [_row("c", text, ep, [1.0, 0.0])],
        dest=str(tmp_path), version=version,
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s", built_at="t",
    )


def _serving_config(monkeypatch):
    monkeypatch.setattr(_config, "EMBED_MODEL", "amazon.titan-embed-text-v2:0")


def test_current_corpus_caches_within_ttl(tmp_path, monkeypatch):
    _serving_config(monkeypatch)
    corpus.reset_serving_cache()
    _publish(tmp_path, "v0001", 251, "first")

    clock = {"t": 1000.0}
    c1 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c1.version == "v0001"

    # Publish a new version, but stay within the TTL window -> cached v0001 returned,
    # current.json is NOT even re-read.
    _publish(tmp_path, "v0002", 252, "second")
    clock["t"] = 1000.0 + 299
    c2 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c2 is c1
    assert c2.version == "v0001"


def test_current_corpus_swaps_after_ttl_when_version_changes(tmp_path, monkeypatch):
    _serving_config(monkeypatch)
    corpus.reset_serving_cache()
    _hybrid._CACHE.clear()
    _publish(tmp_path, "v0001", 251, "byrd rule")

    clock = {"t": 0.0}
    c1 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c1.version == "v0001"

    _publish(tmp_path, "v0002", 252, "tariffs")
    clock["t"] = 400.0  # past the TTL
    c2 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c2.version == "v0002"
    assert c2 is not c1

    # And retrieval reflects the swap (relies on the Task 1 cache-key fix):
    res = _hybrid.hybrid_search(c2, "tariffs", [1.0, 0.0], top_k=1)
    assert res[0]["episode_number"] == 252


def test_current_corpus_keeps_cache_after_ttl_if_version_unchanged(tmp_path, monkeypatch):
    _serving_config(monkeypatch)
    corpus.reset_serving_cache()
    _publish(tmp_path, "v0001", 251, "first")

    clock = {"t": 0.0}
    c1 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    clock["t"] = 400.0  # past TTL, but no new version published
    c2 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c2 is c1  # same object — version unchanged, no reload
