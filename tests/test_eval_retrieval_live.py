"""Live retrieval-quality regression guard over the real corpus artifact.

Asserts hybrid retrieval meets a minimum quality bar, so a future change that
quietly regresses retrieval fails here. Opt-in:

    pytest tests/test_eval_retrieval_live.py -v -m live

Requires PEP_ORACLE_CORPUS_URI (or pass explicitly) and EMBED_BACKEND=bedrock.
"""

import os

import pytest

pytestmark = pytest.mark.live


def test_hybrid_recall_meets_minimum():
    import pep_oracle.eval_retrieval as er
    from pep_oracle.corpus import load_current
    from pep_oracle.embeddings import embed_texts

    corpus_uri = os.environ["PEP_ORACLE_CORPUS_URI"]
    corpus = load_current(corpus_uri)
    res = er.evaluate_corpus(corpus, embed=lambda ts: embed_texts(ts))
    h = res["overall"]
    assert h["recall"][10] >= 0.5, f"hybrid recall@10 too low: {h}"
    assert h["mrr"] >= 0.3, f"hybrid MRR too low: {h}"
