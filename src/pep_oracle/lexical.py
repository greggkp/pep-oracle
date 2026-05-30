"""Lexical (BM25) retrieval primitives — the keyword half of hybrid search.

BM25 complements semantic embeddings: it nails exact/rare terms (proper nouns,
bill names, distinctive phrases) that embeddings blur together, at the cost of
being blind to paraphrase/synonyms. `normalize_numbers` bridges one common
surface-form gap ("day 2" vs "day two").
"""

import math
import re
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9]+")

_NUM_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
    13: "thirteen", 14: "fourteen", 15: "fifteen", 16: "sixteen",
    17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty",
}


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
        self._docs = [tokenize(d) for d in docs]
        self.k1, self.b = k1, b
        self.N = len(self._docs)
        self.avgdl = (sum(len(d) for d in self._docs) / self.N) if self.N else 0.0
        df: Counter = Counter()
        for d in self._docs:
            df.update(set(d))
        self._idf = {w: math.log(1 + (self.N - f + 0.5) / (f + 0.5)) for w, f in df.items()}
        self._tf = [Counter(d) for d in self._docs]

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = [0.0] * self.N
        if not self.avgdl:
            return out
        for i in range(self.N):
            tf = self._tf[i]
            dl = len(self._docs[i])
            s = 0.0
            for w in q:
                f = tf.get(w, 0)
                if f:
                    idf = self._idf.get(w, 0.0)
                    s += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            out[i] = s
        return out
