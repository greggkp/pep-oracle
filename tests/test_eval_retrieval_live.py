"""Live retrieval-quality regression guard over the real corpus.

Asserts hybrid retrieval is at least as good as semantic-only on aggregate, so a
future change that quietly regresses retrieval fails here. Opt-in:

    pytest tests/test_eval_retrieval_live.py -v -m live
"""

import pytest

pytestmark = pytest.mark.live


def test_hybrid_at_least_as_good_as_semantic_overall():
    from pep_oracle.eval_retrieval import run_comparison

    comp = run_comparison()
    s, h = comp["semantic"]["overall"], comp["hybrid"]["overall"]
    assert h["recall"][10] >= s["recall"][10], f"hybrid {h} < semantic {s}"
    assert h["mrr"] >= s["mrr"], f"hybrid {h} < semantic {s}"
