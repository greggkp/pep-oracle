"""Hybrid retrieval: fuse semantic (embedding) and lexical (BM25) rankings.

Embeddings capture meaning/paraphrase; BM25 captures exact/rare terms. Neither
alone is enough (a politics podcast is full of proper nouns the embeddings blur,
and paraphrases BM25 misses), so we rank with both and merge by Reciprocal Rank
Fusion — scale-invariant (no need to normalize cosine vs BM25 scores) and robust
(a chunk both retrievers like floats up).

The BM25 index + corpus is cached in-process and rebuilt when the chunk count
changes (the only writer is the periodic ingest process). Exhaustive local
ranking is used because the corpus is small (≤ ~10k chunks); revisit if it grows.
"""

import math

from pep_oracle.lexical import BM25, normalize_numbers
from pep_oracle.store import SENTINEL_NO_TIME

RRF_K = 60  # Reciprocal Rank Fusion damping constant (standard default)
# Fusion weight on the semantic ranker (BM25 gets 1 - this). Leaning semantic
# means BM25 only sways results when semantic is mediocre — it rescues
# distinctive-term queries without diluting topic queries semantic already nails.
# 0.8 chosen by the eval harness (`pep-oracle eval-retrieval`, n=29): best
# recall@5/@10 with the full specific_term gain and no topic_paraphrase
# regression. Lower weights dilute topic queries; higher loses recall@5.
SEMANTIC_WEIGHT = 0.8

# Per-corpus cache keyed by (name, version) + invalidated on chunk-count change.
# ChromaDB collections have no `.version` (-> None), so the live /ask+MCP-over-Chroma
# behavior is unchanged; InMemoryCorpus carries `.version` so a new artifact swap
# gets a fresh BM25 index instead of colliding with the previous one.
_CACHE: dict = {}  # (name, version) -> {count, ids, docs, embeddings, metas, bm25}


def _load_corpus(collection) -> dict:
    name = collection.name
    version = getattr(collection, "version", None)  # InMemoryCorpus carries a version; Chroma doesn't
    count = collection.count()
    key = (name, version)
    cached = _CACHE.get(key)
    if cached is not None and cached["count"] == count:
        return cached
    got = collection.get(include=["documents", "embeddings", "metadatas"])
    docs = got["documents"]
    corpus = {
        "count": count,
        "ids": got["ids"],
        "docs": docs,
        "embeddings": got["embeddings"],
        "metas": got["metadatas"],
        "bm25": BM25([normalize_numbers(d or "") for d in docs]),
    }
    _CACHE[key] = corpus
    return corpus


def _passes(meta, episode_numbers, after_date, before_date, speaker) -> bool:
    if episode_numbers and meta.get("episode_number") not in episode_numbers:
        return False
    d = meta.get("episode_date", "")
    if after_date and d < after_date:
        return False
    if before_date and d > before_date:
        return False
    if speaker:
        key = f"has_speaker_{speaker.lower().replace(' ', '_')}"
        if not meta.get(key):
            return False
    return True


def _cos(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _rrf(orders: list[list[int]], weights: list[float] | None = None, k: int = RRF_K) -> list[int]:
    if weights is None:
        weights = [1.0] * len(orders)
    score: dict[int, float] = {}
    for order, w in zip(orders, weights):
        for rank, i in enumerate(order):
            score[i] = score.get(i, 0.0) + w / (k + rank + 1)
    return sorted(score, key=lambda i: score[i], reverse=True)


def _to_result(chunk_id, text, m, distance) -> dict:
    st, et = m.get("start_time"), m.get("end_time")
    r = {
        "chunk_id": chunk_id,
        "text": text,
        "distance": distance,
        "episode_guid": m.get("episode_guid"),
        "episode_title": m.get("episode_title"),
        "episode_date": m.get("episode_date"),
        "episode_number": m.get("episode_number"),
        "start_time": None if st == SENTINEL_NO_TIME else st,
        "end_time": None if et == SENTINEL_NO_TIME else et,
    }
    if "speaker_text" in m:
        r["speaker_text"] = m["speaker_text"]
    if "speakers" in m:
        r["speakers"] = m["speakers"]
    return r


def hybrid_search(
    collection,
    query_text: str,
    query_embedding: list[float],
    top_k: int,
    episode_numbers: list[int] | None = None,
    after_date: str | None = None,
    before_date: str | None = None,
    speaker: str | None = None,
    semantic_weight: float = SEMANTIC_WEIGHT,
) -> list[dict]:
    """Return up to ``top_k`` chunks ranked by weighted RRF(semantic, BM25), in
    store.query result shape. ``distance`` is a rank-based proxy (lower = better)
    so the downstream temporal layer treats the fused rank as the relevance
    signal."""
    c = _load_corpus(collection)
    ids, docs, embs, metas, bm25 = c["ids"], c["docs"], c["embeddings"], c["metas"], c["bm25"]

    cand = [i for i in range(len(ids))
            if _passes(metas[i], episode_numbers, after_date, before_date, speaker)]
    if not cand:
        return []

    sem = {i: _cos(query_embedding, embs[i]) for i in cand}
    sem_order = sorted(cand, key=lambda i: sem[i], reverse=True)
    bm_scores = bm25.scores(normalize_numbers(query_text))
    bm_order = sorted(cand, key=lambda i: bm_scores[i], reverse=True)

    fused = _rrf([sem_order, bm_order], weights=[semantic_weight, 1.0 - semantic_weight])
    n = len(fused) or 1
    return [_to_result(ids[i], docs[i], metas[i], distance=rank / n)
            for rank, i in enumerate(fused[:top_k])]
