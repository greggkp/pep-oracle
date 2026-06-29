import json

import pytest

from pep_oracle.lexical import (
    BM25,
    BM25_CODE_FINGERPRINT,
    BM25_INDEX_FORMAT,
    build_bm25,
    normalize_numbers,
    tokenize,
)

_DOCS = [
    "trump said something about the byrd rule reconciliation",
    "trump talked about trade and tariffs",
    "weather sports and general chit chat",
    "day 2 of the senate hearing on immigration",
    "",  # empty doc — must survive round-trip
]
_QUERIES = ["byrd", "trump tariffs", "day two immigration", "absent xyzzy", "trade"]


def _scores(bm, q):
    return bm.scores(normalize_numbers(q))


def test_tokenize():
    assert tokenize("Day 2: Trump's tariffs!") == ["day", "2", "trump", "s", "tariffs"]


def test_normalize_numbers_bridges_digit_word_gap():
    assert normalize_numbers("day 2 and day 4") == "day two and day four"
    assert normalize_numbers("Section 122") == "section 122"  # >20 left as-is
    assert normalize_numbers("round 1, part 3") == "round one, part three"


def test_bm25_ranks_doc_containing_query_term_first():
    docs = ["talk about tariffs and trade", "talk about immigration", "weather today"]
    scores = BM25(docs).scores("tariffs")
    assert scores[0] > scores[1] and scores[0] > scores[2]


def test_bm25_rare_term_outweighs_common_term():
    # "trump" appears in every doc (low IDF); "byrd" in one (high IDF).
    docs = [
        "trump said something about the byrd rule",
        "trump talked about trade",
        "trump and the economy",
    ]
    bm = BM25(docs)
    # A query with the rare term should strongly prefer doc 0.
    s = bm.scores("trump byrd")
    assert s[0] > s[1] and s[0] > s[2]
    # The rare term carries most of the weight: doc 0 beats others even though
    # they also contain "trump".
    only_common = bm.scores("trump")
    assert s[0] - only_common[0] > 0  # byrd added real signal


def test_bm25_length_normalization_prefers_focused_doc():
    short = "tariffs"
    long = "tariffs " + "filler words here " * 50
    s = BM25([short, long]).scores("tariffs")
    assert s[0] > s[1]  # short, on-topic doc wins


def test_bm25_empty_and_missing_terms():
    bm = BM25(["hello world"])
    assert bm.scores("absent") == [0.0]
    assert BM25([]).scores("x") == []


# --- serialization (prebuilt index) ---------------------------------------


def test_bm25_to_dict_from_dict_scores_identical():
    bm = BM25([normalize_numbers(d) for d in _DOCS])
    rebuilt = BM25.from_dict(bm.to_dict())
    for q in _QUERIES:
        # EXACT equality: the reconstructed index must score bit-identically.
        assert _scores(bm, q) == _scores(rebuilt, q)


def test_bm25_from_dict_survives_json_roundtrip():
    # Proves plain-dict tf + IEEE-double round-trip through JSON, the path the
    # corpus sidecar actually takes.
    bm = build_bm25(_DOCS)
    rebuilt = BM25.from_dict(json.loads(json.dumps(bm.to_dict())))
    for q in _QUERIES:
        assert _scores(bm, q) == _scores(rebuilt, q)


def test_build_bm25_matches_manual_and_handles_none():
    docs = ["trump tariffs", None, "byrd rule"]
    expected = BM25([normalize_numbers(d or "") for d in docs])
    got = build_bm25(docs)
    for q in _QUERIES:
        assert _scores(got, q) == _scores(expected, q)


def test_bm25_empty_corpus_roundtrip():
    bm = build_bm25([])
    d = bm.to_dict()
    assert d["N"] == 0
    rebuilt = BM25.from_dict(json.loads(json.dumps(d)))
    assert rebuilt.scores("anything") == []


def test_bm25_from_dict_rejects_bad_index_format():
    d = build_bm25(_DOCS).to_dict()
    d["index_format"] = BM25_INDEX_FORMAT + 1
    with pytest.raises(ValueError, match="index_format"):
        BM25.from_dict(d)


def test_bm25_from_dict_rejects_fingerprint_mismatch():
    d = build_bm25(_DOCS).to_dict()
    d["code_fingerprint"] = "0" * 16  # as if built by different tokenizer/scoring code
    with pytest.raises(ValueError, match="fingerprint"):
        BM25.from_dict(d)


def test_bm25_from_dict_rejects_n_mismatch():
    bm = build_bm25(_DOCS)
    with pytest.raises(ValueError, match="expected"):
        BM25.from_dict(bm.to_dict(), expected_n=len(_DOCS) + 1)
    # Internal length inconsistency is also rejected.
    d = bm.to_dict()
    d["doclen"] = d["doclen"][:-1]
    with pytest.raises(ValueError, match="length mismatch"):
        BM25.from_dict(d)


def test_code_fingerprint_is_stable_and_present():
    # Available in a normal (non-stripped) checkout, and deterministic across calls.
    from pep_oracle.lexical import _compute_code_fingerprint

    assert BM25_CODE_FINGERPRINT is not None
    assert _compute_code_fingerprint() == BM25_CODE_FINGERPRINT


def test_bm25_from_dict_rejects_when_fingerprint_unavailable(monkeypatch):
    # Stripped/zipped deploy: inspect.getsource fails → BM25_CODE_FINGERPRINT is
    # None → every prebuilt index is rejected (fail closed → serving rebuilds).
    import pep_oracle.lexical as lex

    d = build_bm25(_DOCS).to_dict()
    monkeypatch.setattr(lex, "BM25_CODE_FINGERPRINT", None)
    with pytest.raises(ValueError, match="unavailable"):
        BM25.from_dict(d)
