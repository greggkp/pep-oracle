from pep_oracle.lexical import BM25, normalize_numbers, tokenize


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
