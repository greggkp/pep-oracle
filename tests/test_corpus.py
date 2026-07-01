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
        rows,
        dest=str(tmp_path),
        version="v0001",
        embed_model="m",
        dims=2,
        git_sha="s",
        built_at="t",
    )
    assert manifest.episode_range == [None, None]


def test_inmemory_corpus_roundtrip_and_get_shape(tmp_path):
    rows = [
        _row("a", "byrd rule reconciliation", 251, [1.0, 0.0]),
        _row("b", "weather and chit chat", 252, [0.0, 1.0]),
    ]
    corpus.write_artifact(
        rows,
        dest=str(tmp_path),
        version="v0001",
        embed_model="m",
        dims=2,
        git_sha="s",
        built_at="t",
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
        rows,
        dest=str(tmp_path),
        version="v0001",
        embed_model="m",
        dims=2,
        git_sha="s",
        built_at="t",
    )
    c = corpus.load_current(str(tmp_path))

    results = hybrid_search(c, "byrd rule", [1.0, 0.0], top_k=2)
    assert results[0]["chunk_id"] == "a"
    assert set(results[0]) >= {
        "chunk_id",
        "text",
        "distance",
        "episode_guid",
        "episode_title",
        "episode_date",
        "episode_number",
        "start_time",
        "end_time",
    }


def test_load_current_rejects_corrupt_parquet(tmp_path):
    rows = [_row("a", "x", 251, [1.0, 0.0])]
    corpus.write_artifact(
        rows,
        dest=str(tmp_path),
        version="v0001",
        embed_model="m",
        dims=2,
        git_sha="s",
        built_at="t",
    )
    # Corrupt the parquet so its sha256 no longer matches current.json
    (tmp_path / "corpus" / "v0001.parquet").write_bytes(b"corrupted")
    try:
        corpus.load_current(str(tmp_path))
        raise AssertionError("expected a sha256 mismatch error")
    except ValueError as exc:
        assert "sha256" in str(exc).lower()


def test_load_manifest_returns_version_and_manifest(tmp_path):
    rows = [_row("a", "x", 251, [1.0, 0.0]), _row("b", "y", 253, [0.0, 1.0])]
    corpus.write_artifact(
        rows,
        dest=str(tmp_path),
        version="v0007",
        embed_model="amazon.titan-embed-text-v2:0",
        dims=2,
        git_sha="s",
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
        rows,
        dest=str(tmp_path),
        version="v0001",
        embed_model="amazon.titan-embed-text-v2:0",
        dims=2,
        git_sha="s",
        built_at="t",
    )
    monkeypatch.setattr(_config, "EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    c = corpus.load_current(str(tmp_path))
    corpus._validate_serving(c, str(tmp_path))  # no raise


def test_validate_serving_raises_on_embed_model_mismatch(tmp_path, monkeypatch):
    rows = [_row("a", "x", 251, [1.0, 0.0])]
    corpus.write_artifact(
        rows,
        dest=str(tmp_path),
        version="v0001",
        embed_model="amazon.titan-embed-text-v2:0",
        dims=2,
        git_sha="s",
        built_at="t",
    )
    monkeypatch.setattr(_config, "EMBED_MODEL", "some-other-model")  # wrong model vs Titan corpus
    c = corpus.load_current(str(tmp_path))
    try:
        corpus._validate_serving(c, str(tmp_path))
        raise AssertionError("expected an embedder-mismatch error")
    except ValueError as exc:
        assert "embed" in str(exc).lower()


def test_validate_serving_raises_on_dims_mismatch(tmp_path, monkeypatch):
    rows = [_row("a", "x", 251, [1.0, 0.0])]  # 2-d vectors
    corpus.write_artifact(
        rows,
        dest=str(tmp_path),
        version="v0001",
        embed_model="amazon.titan-embed-text-v2:0",
        dims=99,
        git_sha="s",
        built_at="t",  # manifest lies: 99 != 2
    )
    monkeypatch.setattr(_config, "EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    c = corpus.load_current(str(tmp_path))
    try:
        corpus._validate_serving(c, str(tmp_path))
        raise AssertionError("expected a dims-mismatch error")
    except ValueError as exc:
        assert "dim" in str(exc).lower()


def _publish(tmp_path, version, ep, text):
    corpus.write_artifact(
        [_row("c", text, ep, [1.0, 0.0])],
        dest=str(tmp_path),
        version=version,
        embed_model="amazon.titan-embed-text-v2:0",
        dims=2,
        git_sha="s",
        built_at="t",
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


# --- prebuilt BM25 index sidecar ------------------------------------------

from pep_oracle.lexical import build_bm25  # noqa: E402


def _index_rows():
    return [
        _row("a", "byrd rule reconciliation senate vote", 251, [1.0, 0.0]),
        _row("b", "tariffs and trade policy day 2", 252, [0.0, 1.0]),
        _row("c", "weather sports and chit chat", 253, [0.5, 0.5]),
    ]


def _write(tmp_path, rows, version="v0001"):
    corpus.write_artifact(
        rows,
        dest=str(tmp_path),
        version=version,
        embed_model="amazon.titan-embed-text-v2:0",
        dims=2,
        git_sha="s",
        built_at="t",
    )


def test_write_artifact_emits_bm25_sidecar(tmp_path):
    _write(tmp_path, _index_rows())
    sidecar = tmp_path / "corpus" / "v0001.bm25.zst"
    assert sidecar.exists()
    assert sidecar.read_bytes().startswith(corpus._INDEX_MAGIC)
    # Manifest + current.json shape is unchanged (no index keys leaked in).
    cur = json.loads((tmp_path / "corpus" / "current.json").read_text())
    assert set(cur) == {"version", "sha256", "manifest_url"}


def test_serialize_deserialize_roundtrip_scores_identical(tmp_path):
    rows = _index_rows()
    _write(tmp_path, rows)
    sha = json.loads((tmp_path / "corpus" / "current.json").read_text())["sha256"]
    blob = (tmp_path / "corpus" / "v0001.bm25.zst").read_bytes()
    idx = corpus._deserialize_index(blob, parquet_sha=sha, chunk_count=len(rows))
    fresh = build_bm25([r["text"] for r in rows])
    from pep_oracle.lexical import normalize_numbers

    for q in ["byrd rule", "tariffs day two", "weather"]:
        assert idx.scores(normalize_numbers(q)) == fresh.scores(normalize_numbers(q))


def test_load_current_attaches_and_roundtrips_prebuilt(tmp_path):
    rows = _index_rows()
    _write(tmp_path, rows)
    c = corpus.load_current(str(tmp_path))
    assert c.prebuilt_bm25 is not None
    assert c.count() == c.prebuilt_bm25.N
    fresh = build_bm25(list(c.docs))
    from pep_oracle.lexical import normalize_numbers

    for q in ["byrd rule", "tariffs"]:
        assert c.prebuilt_bm25.scores(normalize_numbers(q)) == fresh.scores(normalize_numbers(q))


def test_load_current_falls_back_when_sidecar_missing(tmp_path, monkeypatch):
    _hybrid._CACHE.clear()
    _serving_config(monkeypatch)
    rows = _index_rows()
    _write(tmp_path, rows)
    (tmp_path / "corpus" / "v0001.bm25.zst").unlink()  # simulate a pre-index artifact
    c = corpus.load_current(str(tmp_path))
    assert c.prebuilt_bm25 is None
    res = hybrid_search(c, "byrd rule", [1.0, 0.0], top_k=1)
    assert res[0]["chunk_id"] == "a"  # retrieval still correct via rebuild


def test_load_current_rejects_stale_or_corrupt_sidecar(tmp_path):
    rows = _index_rows()
    _write(tmp_path, rows)
    sidecar = tmp_path / "corpus" / "v0001.bm25.zst"

    # (a) stale: an index whose embedded parquet_sha256 no longer matches.
    stale = corpus._serialize_index(rows, "some-other-parquet-sha")
    sidecar.write_bytes(stale)
    assert corpus.load_current(str(tmp_path)).prebuilt_bm25 is None

    # (b) corrupt: random bytes (bad magic) and a truncated valid frame.
    sidecar.write_bytes(b"not a real index frame at all")
    assert corpus.load_current(str(tmp_path)).prebuilt_bm25 is None
    good = corpus._serialize_index(
        rows, json.loads((tmp_path / "corpus" / "current.json").read_text())["sha256"]
    )
    sidecar.write_bytes(good[: len(good) // 2])
    assert corpus.load_current(str(tmp_path)).prebuilt_bm25 is None

    # (c) valid magic + valid zstd, but the decompressed payload is not JSON
    # (exercises the json.loads-raises path, distinct from the zstd-raises path).
    import struct

    import pyarrow as pa

    payload = b"this is not json"
    bad_json = (
        corpus._INDEX_MAGIC
        + struct.pack("<Q", len(payload))
        + pa.compress(payload, codec="zstd").to_pybytes()
    )
    sidecar.write_bytes(bad_json)
    assert corpus.load_current(str(tmp_path)).prebuilt_bm25 is None


def test_empty_corpus_writes_no_sidecar(tmp_path):
    _write(tmp_path, [])
    assert not (tmp_path / "corpus" / "v0001.bm25.zst").exists()
    assert corpus.load_current(str(tmp_path)).prebuilt_bm25 is None


def test_write_artifact_tolerates_serialize_failure(tmp_path, monkeypatch):
    # A serialize/upload failure must NOT block the publish (serving rebuilds).
    monkeypatch.setattr(
        corpus, "_serialize_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    manifest = corpus.write_artifact(
        _index_rows(),
        dest=str(tmp_path),
        version="v0001",
        embed_model="amazon.titan-embed-text-v2:0",
        dims=2,
        git_sha="s",
        built_at="t",
    )
    assert manifest.chunk_count == 3
    assert (tmp_path / "corpus" / "current.json").exists()  # flip still happened
    assert not (tmp_path / "corpus" / "v0001.bm25.zst").exists()
    assert corpus.load_current(str(tmp_path)).prebuilt_bm25 is None  # serving falls back


def test_ttl_swap_adopts_new_versions_prebuilt_index(tmp_path, monkeypatch):
    # On a TTL version swap the freshly loaded corpus must carry its OWN prebuilt
    # index (not a stale one), so the post-swap search uses it.
    _serving_config(monkeypatch)
    corpus.reset_serving_cache()
    _hybrid._CACHE.clear()
    _publish(tmp_path, "v0001", 251, "byrd rule reconciliation")

    clock = {"t": 0.0}
    c1 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c1.prebuilt_bm25 is not None and c1.version == "v0001"

    _publish(tmp_path, "v0002", 252, "tariffs trade policy")
    clock["t"] = 400.0  # past TTL → swap
    c2 = corpus.current_corpus(str(tmp_path), ttl_seconds=300, now=lambda: clock["t"])
    assert c2 is not c1 and c2.version == "v0002"
    assert c2.prebuilt_bm25 is not None and c2.count() == c2.prebuilt_bm25.N

    res = hybrid_search(c2, "tariffs", [1.0, 0.0], top_k=1)
    assert res[0]["episode_number"] == 252


# --- prebuilt embedding-matrix sidecar --------------------------------------

import numpy as np  # noqa: E402


def test_write_artifact_emits_emb_sidecar(tmp_path):
    _write(tmp_path, _index_rows())
    sidecar = tmp_path / "corpus" / "v0001.emb.zst"
    assert sidecar.exists()
    assert sidecar.read_bytes().startswith(corpus._EMB_MAGIC)
    # Parquet + manifest + current.json are byte-shape unchanged (rollback inert).
    cur = json.loads((tmp_path / "corpus" / "current.json").read_text())
    assert set(cur) == {"version", "sha256", "manifest_url"}


def test_emb_serialize_deserialize_roundtrip_exact(tmp_path):
    rows = _index_rows()
    _write(tmp_path, rows)
    sha = json.loads((tmp_path / "corpus" / "current.json").read_text())["sha256"]
    blob = (tmp_path / "corpus" / "v0001.emb.zst").read_bytes()
    mat = corpus._deserialize_embeddings(blob, parquet_sha=sha)
    expected = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
    assert mat.shape == expected.shape and mat.dtype == np.float32
    assert np.array_equal(mat, expected)  # bit-exact, not approximate


def test_load_current_uses_emb_sidecar_and_matches_parquet_path(tmp_path, monkeypatch):
    _hybrid._CACHE.clear()
    _serving_config(monkeypatch)
    rows = _index_rows()
    _write(tmp_path, rows)

    with_sidecar = corpus.load_current(str(tmp_path))
    (tmp_path / "corpus" / "v0001.emb.zst").unlink()  # simulate a pre-sidecar artifact
    from_parquet = corpus.load_current(str(tmp_path))

    # Same corpus either way: the sidecar is a pure decode-path optimization.
    assert with_sidecar.ids == from_parquet.ids
    assert with_sidecar.docs == from_parquet.docs
    assert with_sidecar.metas == from_parquet.metas
    assert np.array_equal(np.asarray(with_sidecar.embeddings), np.asarray(from_parquet.embeddings))
    # The sidecar matrix is a read-only frombuffer view; the full retrieval path
    # (cosine + BM25 + RRF) must work on it unchanged.
    res = hybrid_search(with_sidecar, "byrd rule", [1.0, 0.0], top_k=1)
    assert res[0]["chunk_id"] == "a"


def test_load_current_rejects_stale_or_corrupt_emb_sidecar(tmp_path):
    rows = _index_rows()
    _write(tmp_path, rows)
    sidecar = tmp_path / "corpus" / "v0001.emb.zst"
    expected = np.asarray([r["embedding"] for r in rows], dtype=np.float32)

    # (a) stale: matrix whose embedded parquet sha no longer matches.
    sidecar.write_bytes(corpus._serialize_embeddings(rows, "0" * 64))
    assert np.array_equal(np.asarray(corpus.load_current(str(tmp_path)).embeddings), expected)

    # (b) corrupt: bad magic, then a truncated valid frame.
    sidecar.write_bytes(b"definitely not an embeddings frame")
    assert np.array_equal(np.asarray(corpus.load_current(str(tmp_path)).embeddings), expected)
    sha = json.loads((tmp_path / "corpus" / "current.json").read_text())["sha256"]
    good = corpus._serialize_embeddings(rows, sha)
    sidecar.write_bytes(good[: len(good) // 2])
    assert np.array_equal(np.asarray(corpus.load_current(str(tmp_path)).embeddings), expected)


def test_emb_sidecar_row_count_mismatch_falls_back_to_column(tmp_path):
    # A frame that passes the sha check but carries the wrong number of rows must
    # be rejected by the parse-time row-count guard, which then re-reads the
    # embedding column it originally skipped.
    rows = _index_rows()
    _write(tmp_path, rows)
    sha = json.loads((tmp_path / "corpus" / "current.json").read_text())["sha256"]
    short = corpus._serialize_embeddings(rows[:2], sha)  # valid frame, 2 rows not 3
    (tmp_path / "corpus" / "v0001.emb.zst").write_bytes(short)
    c = corpus.load_current(str(tmp_path))
    expected = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
    assert np.array_equal(np.asarray(c.embeddings), expected)


def test_empty_corpus_writes_no_emb_sidecar(tmp_path):
    _write(tmp_path, [])
    assert not (tmp_path / "corpus" / "v0001.emb.zst").exists()
    assert corpus.load_current(str(tmp_path)).count() == 0


def test_write_artifact_tolerates_emb_serialize_failure(tmp_path, monkeypatch):
    # An embeddings-sidecar failure must NOT block the publish or the BM25 sidecar.
    monkeypatch.setattr(
        corpus,
        "_serialize_embeddings",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _write(tmp_path, _index_rows())
    assert (tmp_path / "corpus" / "current.json").exists()  # flip still happened
    assert (tmp_path / "corpus" / "v0001.bm25.zst").exists()  # bm25 unaffected
    assert not (tmp_path / "corpus" / "v0001.emb.zst").exists()
    c = corpus.load_current(str(tmp_path))  # serving falls back to the column
    assert c.count() == 3 and c.prebuilt_bm25 is not None
