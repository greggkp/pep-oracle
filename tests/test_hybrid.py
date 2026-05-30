import pep_oracle.hybrid as hybrid
from pep_oracle.hybrid import hybrid_search
from pep_oracle.models import Chunk
from pep_oracle.store import add_chunks, get_client

_counter = 0


def _fresh_collection():
    global _counter
    _counter += 1
    hybrid._CACHE.clear()  # avoid cross-test corpus cache bleed
    client = get_client(persistent=False)
    return client.get_or_create_collection(
        name=f"test_hybrid_{_counter}", metadata={"hnsw:space": "cosine"}
    )


def _chunk(cid, text, ep=200, date="2026-01-01", speaker_turns=None):
    return Chunk(
        chunk_id=cid, episode_guid=f"g{ep}", text=text,
        episode_title=f"Ep {ep}", episode_date=date,
        start_time=0.0, end_time=10.0, episode_number=ep,
        speaker_turns=speaker_turns,
    )


def test_bm25_rescues_lexically_relevant_but_semantically_distant_chunk():
    col = _fresh_collection()
    chunks = [
        _chunk("a", "the byrd rule reconciliation senate vote"),  # has query terms
        _chunk("b", "weather sports and general chit chat"),      # no query terms
        _chunk("c", "another off-topic filler chunk here"),
    ]
    # Embeddings: query is close to b/c, ORTHOGONAL to a -> semantic buries 'a'.
    embeddings = [[0.0, 1.0], [1.0, 0.1], [0.9, 0.2]]
    add_chunks(col, chunks, embeddings)

    # Exercise the fusion MECHANISM with balanced weights (the production
    # default leans semantic, validated separately by the eval harness): 'a' is
    # orthogonal to the query embedding (semantic rank last) yet BM25 ranks it
    # #1, so balanced RRF must surface it into the top 2.
    results = hybrid_search(col, "byrd rule", [1.0, 0.0], top_k=3, semantic_weight=0.5)
    ids = [r["chunk_id"] for r in results]
    assert "a" in ids[:2]


def test_filters_episode_date_speaker():
    col = _fresh_collection()
    chunks = [
        _chunk("e1", "tariffs talk", ep=260, date="2026-05-01",
               speaker_turns=[{"speaker": "Chas", "start": 0.0, "end": 10.0}]),
        _chunk("e2", "tariffs talk", ep=200, date="2025-01-01",
               speaker_turns=[{"speaker": "Dave", "start": 0.0, "end": 10.0}]),
    ]
    add_chunks(col, chunks, [[1.0, 0.0], [1.0, 0.0]])

    by_ep = hybrid_search(col, "tariffs", [1.0, 0.0], top_k=5, episode_numbers=[260])
    assert [r["chunk_id"] for r in by_ep] == ["e1"]

    by_date = hybrid_search(col, "tariffs", [1.0, 0.0], top_k=5, after_date="2026-01-01")
    assert [r["chunk_id"] for r in by_date] == ["e1"]

    by_speaker = hybrid_search(col, "tariffs", [1.0, 0.0], top_k=5, speaker="Dave")
    assert [r["chunk_id"] for r in by_speaker] == ["e2"]


def test_result_shape_matches_store_query():
    col = _fresh_collection()
    add_chunks(col, [_chunk("x", "hello world")], [[1.0, 0.0]])
    r = hybrid_search(col, "hello", [1.0, 0.0], top_k=1)[0]
    assert set(r) >= {"chunk_id", "text", "distance", "episode_guid",
                      "episode_title", "episode_date", "episode_number",
                      "start_time", "end_time"}
    assert r["start_time"] == 0.0


def test_corpus_cache_rebuilds_when_count_changes():
    col = _fresh_collection()
    add_chunks(col, [_chunk("a", "tariffs")], [[1.0, 0.0]])
    assert len(hybrid_search(col, "tariffs", [1.0, 0.0], top_k=5)) == 1
    add_chunks(col, [_chunk("b", "tariffs again", ep=201)], [[1.0, 0.0]])
    # count changed -> cache rebuilt -> new chunk visible
    assert len(hybrid_search(col, "tariffs", [1.0, 0.0], top_k=5)) == 2


def test_empty_collection_returns_empty():
    col = _fresh_collection()
    assert hybrid_search(col, "anything", [1.0, 0.0], top_k=5) == []
