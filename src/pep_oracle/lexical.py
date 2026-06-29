"""Lexical (BM25) retrieval primitives — the keyword half of hybrid search.

BM25 complements semantic embeddings: it nails exact/rare terms (proper nouns,
bill names, distinctive phrases) that embeddings blur together, at the cost of
being blind to paraphrase/synonyms. `normalize_numbers` bridges one common
surface-form gap ("day 2" vs "day two").

The built index can be serialized (`BM25.to_dict`/`from_dict`) so the corpus
artifact can ship a prebuilt index and the serving path skips the ~2.7s cold-start
rebuild (see `corpus.py` sidecar + `docs/aws/cold-path-measurement.md`).
"""

from __future__ import annotations

import hashlib
import inspect
import math
import re
from collections import Counter

# Bump on any INTENTIONAL change to the serialized index layout. (Behavioural
# changes to tokenize/normalize_numbers/scores are caught automatically by
# BM25_CODE_FINGERPRINT below, so this is only for layout, not formula, changes.)
BM25_INDEX_FORMAT = 1

_TOKEN = re.compile(r"[a-z0-9]+")

_NUM_WORDS = {
    0: "zero",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
    20: "twenty",
}


# NOTE: tokenize / normalize_numbers / build_bm25 / BM25.__init__ / BM25.scores
# (plus _TOKEN and _NUM_WORDS) define how the index is built and queried — the
# build side bakes idf/avgdl/doclen into the serialized state, the query side
# consumes them. Any behavioural change here invalidates serialized BM25 indexes;
# that is caught automatically by BM25_CODE_FINGERPRINT (which folds in all of
# that source), so a stale index is rejected and rebuilt rather than mis-scoring.
def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def normalize_numbers(text: str) -> str:
    """Map standalone small digits to their word form so lexical matching sees
    "day 2" and "day two" as the same (only 0-20 — the common case; larger
    numbers like "Section 122" are identifiers and kept verbatim)."""
    return re.sub(
        r"\b\d{1,2}\b",
        lambda m: _NUM_WORDS.get(int(m.group()), m.group()),
        text.lower(),
    )


class BM25:
    """Okapi BM25 over a fixed document set. `scores(query)` returns one score
    per document, aligned to the input order."""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        toks = [tokenize(d) for d in docs]  # local: only needed to derive tf/df/doclen
        self.k1, self.b = k1, b
        self.N = len(toks)
        self.avgdl = (sum(len(d) for d in toks) / self.N) if self.N else 0.0
        df: Counter = Counter()
        for d in toks:
            df.update(set(d))
        self._idf = {w: math.log(1 + (self.N - f + 0.5) / (f + 0.5)) for w, f in df.items()}
        self._tf = [Counter(d) for d in toks]
        # Per-doc length is all scores() needs from the tokenized docs, so we keep
        # only this (not the token lists) — which also lets from_dict rebuild a
        # scorable instance without re-tokenizing.
        self._doclen = [len(d) for d in toks]

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = [0.0] * self.N
        if not self.avgdl:
            return out
        for i in range(self.N):
            tf = self._tf[i]
            dl = self._doclen[i]
            s = 0.0
            for w in q:
                f = tf.get(w, 0)
                if f:
                    idf = self._idf.get(w, 0.0)
                    s += (
                        idf
                        * (f * (self.k1 + 1))
                        / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
                    )
            out[i] = s
        return out

    def to_dict(self) -> dict:
        """Serialize the built index so it can be reloaded without re-tokenizing.
        `BM25.from_dict(self.to_dict())` scores identically to ``self``. Carries a
        format tag + code fingerprint so a reload can reject a mismatching index."""
        return {
            "index_format": BM25_INDEX_FORMAT,
            "code_fingerprint": BM25_CODE_FINGERPRINT,
            "k1": self.k1,
            "b": self.b,
            "N": self.N,
            "avgdl": self.avgdl,
            "idf": self._idf,
            "tf": [dict(c) for c in self._tf],
            "doclen": self._doclen,
        }

    @classmethod
    def from_dict(cls, d: dict, *, expected_n: int | None = None) -> BM25:
        """Reconstruct a scorable BM25 from ``to_dict()`` output WITHOUT
        re-tokenizing. Raises ``ValueError`` if the index can't be trusted — wrong
        layout (``index_format``), a tokenizer/scoring change since it was built
        (``code_fingerprint``), or a size mismatch — so callers fall back to a
        fresh build. ``scores()`` reads ``_tf`` via ``.get`` and ``_doclen``, both
        of which survive a JSON round-trip as plain ``dict``/``list``."""
        if d.get("index_format") != BM25_INDEX_FORMAT:
            raise ValueError(f"bm25 index_format {d.get('index_format')!r} != {BM25_INDEX_FORMAT}")
        if BM25_CODE_FINGERPRINT is None or d.get("code_fingerprint") != BM25_CODE_FINGERPRINT:
            raise ValueError("bm25 code_fingerprint mismatch or unavailable")
        obj = cls.__new__(cls)  # bypass __init__: do NOT re-tokenize
        obj.k1 = d["k1"]
        obj.b = d["b"]
        obj.N = d["N"]
        obj.avgdl = d["avgdl"]
        obj._idf = d["idf"]
        obj._tf = d["tf"]
        obj._doclen = d["doclen"]
        if len(obj._tf) != obj.N or len(obj._doclen) != obj.N:
            raise ValueError(
                f"bm25 index length mismatch: N={obj.N} tf={len(obj._tf)} doclen={len(obj._doclen)}"
            )
        if expected_n is not None and expected_n != obj.N:
            raise ValueError(f"bm25 index N={obj.N} != expected {expected_n}")
        return obj


def build_bm25(docs: list[str | None]) -> BM25:
    """Canonical BM25 builder used by BOTH the artifact write/serialize path and
    the serving fallback, so a prebuilt index can never drift from the runtime
    preprocessing (number-normalized, None→"")."""
    return BM25([normalize_numbers(d or "") for d in docs])


def _compute_code_fingerprint() -> str | None:
    """Fingerprint everything that defines the index's numbers so a serialized
    index built by a different code version (e.g. serving deployed ahead of the
    ingest that built it) is auto-rejected and rebuilt rather than silently
    mis-scored. Covers BOTH sides of the equivalence: how the index is BUILT
    (`build_bm25` preprocessing + `BM25.__init__`, which bakes idf/avgdl/doclen
    into the serialized state) AND how it is QUERIED (`tokenize`,
    `normalize_numbers`, `BM25.scores`) — plus the module-level `_TOKEN` regex and
    `_NUM_WORDS` table the function source alone would miss. A change to the idf or
    avgdl formula therefore invalidates stale indexes even though `scores()` is
    unchanged. Computed ONCE at import (lands in the Lambda Init phase, off the
    cold request path); returns None when source is unavailable (zipped/stripped
    deploy) so `from_dict` fails closed → rebuild."""
    try:
        src = (
            inspect.getsource(tokenize)
            + inspect.getsource(normalize_numbers)
            + inspect.getsource(build_bm25)
            + inspect.getsource(BM25.__init__)
            + inspect.getsource(BM25.scores)
            + _TOKEN.pattern
            + repr(sorted(_NUM_WORDS.items()))
        )
    except (OSError, TypeError):
        return None
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


BM25_CODE_FINGERPRINT = _compute_code_fingerprint()
