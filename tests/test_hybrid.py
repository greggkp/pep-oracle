import pytest

import pep_oracle.corpus as corpus
import pep_oracle.hybrid as hybrid
from pep_oracle.hybrid import hybrid_search
from pep_oracle.models import Chunk
from pep_oracle.store import _chunk_metadata

_counter = 0


def _chunk(cid, text, ep=200, date="2026-01-01", speaker_turns=None):
    return Chunk(
        chunk_id=cid,
        episode_guid=f"g{ep}",
        text=text,
        episode_title=f"Ep {ep}",
        episode_date=date,
        start_time=0.0,
        end_time=10.0,
        episode_number=ep,
        speaker_turns=speaker_turns,
    )


def _row(chunk, embedding):
    # Build a corpus row from a Chunk using the canonical prod metadata builder,
    # so speaker (has_speaker_*) and time-sentinel fields match what ingest writes.
    return {
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "embedding": embedding,
        "metadata": _chunk_metadata(chunk),
    }


def _corpus(tmp_path, chunks, embeddings, version="v0001"):
    """Build an InMemoryCorpus (the prod serving type) from Chunks + embeddings.

    Drop-in for hybrid_search, replacing the old ephemeral ChromaDB collection.
    Clears the hybrid corpus cache to avoid cross-test bleed.
    """
    global _counter
    _counter += 1
    hybrid._CACHE.clear()
    rows = [_row(c, e) for c, e in zip(chunks, embeddings, strict=False)]
    dest = tmp_path / f"c{_counter}"
    corpus.write_artifact(
        rows,
        dest=str(dest),
        version=version,
        embed_model="m",
        dims=len(embeddings[0]),
        git_sha="s",
        built_at="t",
    )
    return corpus.load_current(str(dest))


def test_bm25_rescues_lexically_relevant_but_semantically_distant_chunk(tmp_path):
    chunks = [
        _chunk("a", "the byrd rule reconciliation senate vote"),  # has query terms
        _chunk("b", "weather sports and general chit chat"),  # no query terms
        _chunk("c", "another off-topic filler chunk here"),
    ]
    # Embeddings: query is close to b/c, ORTHOGONAL to a -> semantic buries 'a'.
    embeddings = [[0.0, 1.0], [1.0, 0.1], [0.9, 0.2]]
    col = _corpus(tmp_path, chunks, embeddings)

    # Exercise the fusion MECHANISM with balanced weights (the production
    # default leans semantic, validated separately by the eval harness): 'a' is
    # orthogonal to the query embedding (semantic rank last) yet BM25 ranks it
    # #1, so balanced RRF must surface it into the top 2.
    results = hybrid_search(col, "byrd rule", [1.0, 0.0], top_k=3, semantic_weight=0.5)
    ids = [r["chunk_id"] for r in results]
    assert "a" in ids[:2]


def test_filters_episode_date_speaker(tmp_path):
    chunks = [
        _chunk(
            "e1",
            "tariffs talk",
            ep=260,
            date="2026-05-01",
            speaker_turns=[{"speaker": "Chas", "start": 0.0, "end": 10.0}],
        ),
        _chunk(
            "e2",
            "tariffs talk",
            ep=200,
            date="2025-01-01",
            speaker_turns=[{"speaker": "Dave", "start": 0.0, "end": 10.0}],
        ),
    ]
    col = _corpus(tmp_path, chunks, [[1.0, 0.0], [1.0, 0.0]])

    by_ep = hybrid_search(col, "tariffs", [1.0, 0.0], top_k=5, episode_numbers=[260])
    assert [r["chunk_id"] for r in by_ep] == ["e1"]

    by_date = hybrid_search(col, "tariffs", [1.0, 0.0], top_k=5, after_date="2026-01-01")
    assert [r["chunk_id"] for r in by_date] == ["e1"]

    by_speaker = hybrid_search(col, "tariffs", [1.0, 0.0], top_k=5, speaker="Dave")
    assert [r["chunk_id"] for r in by_speaker] == ["e2"]


def test_result_shape_matches_store_query(tmp_path):
    col = _corpus(tmp_path, [_chunk("x", "hello world")], [[1.0, 0.0]])
    r = hybrid_search(col, "hello", [1.0, 0.0], top_k=1)[0]
    assert set(r) >= {
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
    assert r["start_time"] == 0.0


def test_corpus_cache_rebuilds_on_new_version(tmp_path):
    # The hybrid cache is keyed on (name, version); publishing v0002 (which the
    # InMemoryCorpus carries as .version) forces a fresh BM25 index, so the new
    # chunk is visible — the InMemoryCorpus analogue of ChromaDB's count change.
    chunks1 = [_chunk("a", "tariffs")]
    col1 = _corpus(tmp_path, chunks1, [[1.0, 0.0]], version="v0001")
    assert len(hybrid_search(col1, "tariffs", [1.0, 0.0], top_k=5)) == 1

    chunks2 = [_chunk("a", "tariffs"), _chunk("b", "tariffs again", ep=201)]
    col2 = _corpus(tmp_path, chunks2, [[1.0, 0.0], [1.0, 0.0]], version="v0002")
    # new version -> cache rebuilt -> the added chunk is visible
    assert len(hybrid_search(col2, "tariffs", [1.0, 0.0], top_k=5)) == 2


def test_empty_collection_returns_empty(tmp_path):
    hybrid._CACHE.clear()
    corpus.write_artifact(
        [],
        dest=str(tmp_path),
        version="v0001",
        embed_model="m",
        dims=2,
        git_sha="s",
        built_at="t",
    )
    col = corpus.load_current(str(tmp_path))
    assert col.count() == 0
    assert hybrid_search(col, "anything", [1.0, 0.0], top_k=5) == []


def test_cache_keys_on_version_not_just_name():
    """Two InMemoryCorpus instances share the name 'pep_oracle' and the same chunk
    count; only `.version` differs. The cache must NOT serve the first one's data
    for the second (the bug that would defeat the serving-path atomic swap)."""
    from pep_oracle.corpus import InMemoryCorpus

    hybrid._CACHE.clear()

    def _meta(ep):
        return {
            "episode_number": ep,
            "episode_date": f"2026-01-0{ep}",
            "episode_guid": f"g{ep}",
            "episode_title": f"Ep {ep}",
            "start_time": 0.0,
            "end_time": 1.0,
        }

    a = InMemoryCorpus(
        ["a"], ["byrd rule reconciliation"], [[1.0, 0.0]], [_meta(1)], version="v0001"
    )
    b = InMemoryCorpus(["b"], ["tariffs section 122"], [[1.0, 0.0]], [_meta(2)], version="v0002")

    ra = hybrid_search(a, "byrd rule", [1.0, 0.0], top_k=1)
    rb = hybrid_search(b, "tariffs", [1.0, 0.0], top_k=1)

    assert ra[0]["chunk_id"] == "a"
    assert rb[0]["chunk_id"] == "b"  # not stale 'a' from a name-only cache key


# --- prebuilt BM25 index adoption -----------------------------------------


def test_uses_prebuilt_index_and_skips_rebuild(tmp_path, monkeypatch):
    """A corpus carrying a prebuilt index must be used WITHOUT calling build_bm25,
    and must rank identically to the rebuild path."""
    chunks = [
        _chunk("a", "the byrd rule reconciliation senate vote"),
        _chunk("b", "weather sports and general chit chat"),
    ]
    embeddings = [[1.0, 0.0], [0.0, 1.0]]
    col = _corpus(tmp_path, chunks, embeddings)  # load_current attaches prebuilt_bm25
    assert col.prebuilt_bm25 is not None

    hybrid._CACHE.clear()
    monkeypatch.setattr(
        hybrid,
        "build_bm25",
        lambda *a, **k: pytest.fail("build_bm25 called despite prebuilt index"),
    )
    # Two calls: the first populates the (name, version) cache; the second must
    # reuse it. Neither may rebuild (build_bm25 is rigged to fail above).
    res = hybrid_search(col, "byrd rule", [1.0, 0.0], top_k=1)
    assert res[0]["chunk_id"] == "a"
    res2 = hybrid_search(col, "byrd rule", [1.0, 0.0], top_k=1)
    assert res2[0]["chunk_id"] == "a"


def test_falls_back_to_build_when_no_prebuilt(tmp_path):
    """A directly-constructed corpus (prebuilt_bm25=None) ranks via rebuild."""
    from pep_oracle.corpus import InMemoryCorpus

    hybrid._CACHE.clear()
    meta = {
        "episode_number": 1,
        "episode_date": "2026-01-01",
        "episode_guid": "g1",
        "episode_title": "Ep 1",
        "start_time": 0.0,
        "end_time": 1.0,
    }
    col = InMemoryCorpus(["a"], ["byrd rule reconciliation"], [[1.0, 0.0]], [meta], version="v0001")
    assert col.prebuilt_bm25 is None
    res = hybrid_search(col, "byrd rule", [1.0, 0.0], top_k=1)
    assert res[0]["chunk_id"] == "a"


def test_stale_prebuilt_count_triggers_rebuild(tmp_path, monkeypatch):
    """A prebuilt index whose N disagrees with the corpus count is ignored (the
    defense-in-depth guard), and search rebuilds and still returns correct results."""
    chunks = [_chunk("a", "byrd rule reconciliation"), _chunk("b", "tariffs trade")]
    col = _corpus(tmp_path, chunks, [[1.0, 0.0], [0.0, 1.0]])
    col.prebuilt_bm25.N = 999  # corrupt the adopted index's size
    hybrid._CACHE.clear()
    built = {"n": 0}
    real_build = hybrid.build_bm25

    def _counting_build(docs):
        built["n"] += 1
        return real_build(docs)

    monkeypatch.setattr(hybrid, "build_bm25", _counting_build)
    res = hybrid_search(col, "byrd rule", [1.0, 0.0], top_k=1)
    assert built["n"] == 1  # rebuilt rather than trusting the mismatched index
    assert res[0]["chunk_id"] == "a"
