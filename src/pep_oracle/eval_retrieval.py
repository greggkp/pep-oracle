"""Retrieval evaluation harness.

Measures retrieval quality on a small, type-tagged query set against the real
corpus, so changes (hybrid vs semantic, future rerankers) are judged by data
rather than by anecdote. Ground truth is defined by a distinctive PHRASE that
provably appears in the relevant episode(s) — resolved from the corpus at run
time, so the labels can't drift. Metrics: recall@k and MRR (episode-level).

Run: `pep-oracle eval-retrieval`.
"""

# Each case: a realistic user query, its type, and a distinctive phrase whose
# containing episode(s) are the ground-truth relevant set. For *_paraphrase
# cases the query deliberately avoids the literal phrase, to test real retrieval
# rather than lexical echo.
CASES = [
    # specific term — BM25's strength (query includes the distinctive term)
    {"query": "reconciliation byrd rule senate", "type": "specific_term", "phrase": "byrd rule"},
    {"query": "Section 122 tariffs", "type": "specific_term", "phrase": "section 122"},
    {"query": "golden dome missile defense", "type": "specific_term", "phrase": "golden dome"},
    {"query": "the Signal chat leak", "type": "specific_term", "phrase": "signal chat"},
    # topic paraphrase — semantic (literal phrase withheld from the query)
    {"query": "the tariff law the administration is invoking", "type": "topic_paraphrase", "phrase": "section 122"},
    {"query": "the missile defense shield Trump announced", "type": "topic_paraphrase", "phrase": "golden dome"},
    {"query": "officials leaked military strike plans in a group chat", "type": "topic_paraphrase", "phrase": "signal chat"},
    {"query": "Trump comparing himself to a boxer, the madman strategy", "type": "topic_paraphrase", "phrase": "mike tyson"},
    {"query": "the US trying to take over the Panama Canal", "type": "topic_paraphrase", "phrase": "panama canal"},
    # number paraphrase — exercises digit->word normalization
    {"query": "Trump gave Iran 2 or 3 days to make a deal", "type": "number_paraphrase", "phrase": "two or three days"},
    # coined phrase — a known-hard, idiosyncratic target
    {"query": "Trump conflict handling pattern day 2 day 4", "type": "coined_phrase", "phrase": "day two of the cycle"},

    # --- expanded set (phrases verified present in the corpus) ---
    # specific_term (query carries the distinctive term)
    {"query": "the Houthis in Yemen", "type": "specific_term", "phrase": "houthis"},
    {"query": "Mike Waltz national security adviser", "type": "specific_term", "phrase": "mike waltz"},
    {"query": "Alligator Alcatraz detention facility", "type": "specific_term", "phrase": "alligator alcatraz"},
    {"query": "Kash Patel at the FBI", "type": "specific_term", "phrase": "kash patel"},
    {"query": "the madman theory of negotiation", "type": "specific_term", "phrase": "madman theory"},
    {"query": "the debt ceiling fight", "type": "specific_term", "phrase": "debt ceiling"},
    {"query": "John Fetterman", "type": "specific_term", "phrase": "fetterman"},
    {"query": "the No Kings protests", "type": "specific_term", "phrase": "no kings"},
    # topic_paraphrase (concept described; distinctive phrase withheld)
    {"query": "Yemeni rebels attacking Red Sea shipping", "type": "topic_paraphrase", "phrase": "houthis"},
    {"query": "the immigration detention camp in the Florida Everglades", "type": "topic_paraphrase", "phrase": "alligator alcatraz"},
    {"query": "mass nationwide street protests against Trump", "type": "topic_paraphrase", "phrase": "no kings"},
    {"query": "Musk's government cost-cutting task force", "type": "topic_paraphrase", "phrase": "doge"},
    {"query": "ending automatic citizenship for US-born children of immigrants", "type": "topic_paraphrase", "phrase": "birthright citizenship"},
    {"query": "the new FBI director Trump appointed", "type": "topic_paraphrase", "phrase": "kash patel"},
    {"query": "the Ukrainian president's tense White House meeting", "type": "topic_paraphrase", "phrase": "volodymyr zelensky"},
    # multi_episode (broad recurring topic; recall any relevant episode)
    {"query": "Iran's nuclear program", "type": "multi_episode", "phrase": "iran nuclear"},
    {"query": "Putin and the war in Ukraine", "type": "multi_episode", "phrase": "vladimir putin"},
    {"query": "birthright citizenship executive order", "type": "multi_episode", "phrase": "birthright citizenship"},
]


def resolve_relevant_episodes(docs, metas, phrase: str) -> set:
    p = phrase.lower()
    return {
        metas[i].get("episode_number")
        for i in range(len(docs))
        if p in (docs[i] or "").lower() and metas[i].get("episode_number")
    }


def recall_at_k(result_episodes: list, relevant: set, k: int) -> int:
    return 1 if relevant and any(e in relevant for e in result_episodes[:k]) else 0


def reciprocal_rank(result_episodes: list, relevant: set) -> float:
    for rank, e in enumerate(result_episodes, start=1):
        if e in relevant:
            return 1.0 / rank
    return 0.0


def aggregate(per_case: list[dict], ks: list[int]) -> dict:
    n = len(per_case) or 1
    return {
        "recall": {k: sum(c["recall"][k] for c in per_case) / n for k in ks},
        "mrr": sum(c["rr"] for c in per_case) / n,
        "n": len(per_case),
    }


def evaluate(retriever_fn, docs, metas, cases=CASES, ks=(5, 10)) -> dict:
    """retriever_fn(query: str, top_k: int) -> list of result dicts (each with
    'episode_number'). Returns {"overall": agg, "by_type": {type: agg}, "cases": [...]}."""
    kmax = max(ks)
    per_case = []
    for case in cases:
        relevant = resolve_relevant_episodes(docs, metas, case["phrase"])
        results = retriever_fn(case["query"], kmax)
        eps = [r.get("episode_number") for r in results]
        per_case.append({
            "query": case["query"], "type": case["type"], "relevant": sorted(relevant),
            "recall": {k: recall_at_k(eps, relevant, k) for k in ks},
            "rr": reciprocal_rank(eps, relevant),
        })
    by_type: dict = {}
    for c in per_case:
        by_type.setdefault(c["type"], []).append(c)
    return {
        "overall": aggregate(per_case, list(ks)),
        "by_type": {t: aggregate(cs, list(ks)) for t, cs in by_type.items()},
        "cases": per_case,
    }


def _semantic_retriever(collection):
    from pep_oracle.embeddings import embed_texts
    from pep_oracle.store import query as store_query

    def fn(query, top_k):
        return store_query(collection, embed_texts([query])[0], top_k=top_k)
    return fn


def _hybrid_retriever(collection):
    from pep_oracle.embeddings import embed_texts
    from pep_oracle.hybrid import hybrid_search

    def fn(query, top_k):
        return hybrid_search(collection, query, embed_texts([query])[0], top_k=top_k)
    return fn


def run_comparison(ks=(5, 10)) -> dict:
    """Evaluate semantic-only vs hybrid on the live corpus. Returns
    {retriever_name: evaluate(...)}."""
    from pep_oracle.store import get_fresh_collection

    collection = get_fresh_collection()
    got = collection.get(include=["documents", "metadatas"])
    docs, metas = got["documents"], got["metadatas"]
    return {
        "semantic": evaluate(_semantic_retriever(collection), docs, metas, ks=ks),
        "hybrid": evaluate(_hybrid_retriever(collection), docs, metas, ks=ks),
    }


def format_report(comparison: dict, ks=(5, 10)) -> str:
    lines = []
    hdr = "retriever  " + "  ".join(f"recall@{k}" for k in ks) + "   MRR"
    lines.append("=== OVERALL ===")
    lines.append(hdr)
    for name, res in comparison.items():
        o = res["overall"]
        cells = "   ".join(f"{o['recall'][k]:.2f}    " for k in ks)
        lines.append(f"{name:9}  {cells}  {o['mrr']:.2f}  (n={o['n']})")
    # per-type, side by side
    types = list(next(iter(comparison.values()))["by_type"])
    lines.append("\n=== recall@%d by query type (semantic -> hybrid) ===" % ks[-1])
    for t in types:
        s = comparison["semantic"]["by_type"][t]["recall"][ks[-1]]
        h = comparison["hybrid"]["by_type"][t]["recall"][ks[-1]]
        lines.append(f"  {t:18} {s:.2f} -> {h:.2f}  (n={comparison['hybrid']['by_type'][t]['n']})")
    return "\n".join(lines)
