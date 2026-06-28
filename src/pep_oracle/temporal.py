"""Intent-gated temporal selection/reranking for retrieved chunks.

Recency is applied ONLY for the 'current' intent. A global recency prior makes
old-but-relevant content statistically unretrievable (the "temporal event
horizon"), which would break 'historical', 'evolution', and 'prediction'
questions — so those keep pure relevance and differ only in presentation order.

The intent is supplied by the MCP tool's caller (a frontier model) as a
caller-set parameter.
"""

from datetime import date

HALF_LIFE_DAYS = 21  # recency half-life for 'current' (tune via eval)
CANDIDATE_MULTIPLIER = 4  # candidate pool = top_k * this, then reranked here
CURRENT_RECENCY_WEIGHT = 0.5  # blend of recency vs similarity for 'current'
MAX_PER_EPISODE_EVOLUTION = 2

NEWEST_FIRST = "newest_first"
CHRONOLOGICAL = "chronological"

VALID_INTENTS = {"current", "historical", "evolution", "prediction", "timeless"}


def recency_score(episode_date: str, today: date, half_life_days: int = HALF_LIFE_DAYS) -> float:
    """Exponential decay in [0, 1]: 1.0 today, 0.5 one half-life ago, →0 when old."""
    try:
        d = date.fromisoformat(episode_date)
    except (ValueError, TypeError):
        return 0.0
    age = (today - d).days
    if age <= 0:
        return 1.0
    return 0.5 ** (age / half_life_days)


def _by_similarity(candidates: list[dict]) -> list[dict]:
    return sorted(candidates, key=lambda c: c.get("distance", 1.0))


def _by_date(candidates: list[dict], reverse: bool) -> list[dict]:
    return sorted(candidates, key=lambda c: c.get("episode_date", ""), reverse=reverse)


def select_for_intent(
    candidates: list[dict],
    intent: str | None,
    top_k: int,
    today: date,
    half_life_days: int = HALF_LIFE_DAYS,
) -> tuple[list[dict], str]:
    """Pick the final ``top_k`` from a similarity-ranked candidate pool and decide
    presentation order, gated by ``intent``. Returns (results, order) where order
    is NEWEST_FIRST or CHRONOLOGICAL (the caller renders in that order).
    """
    cands = _by_similarity(candidates)
    if not cands:
        return [], NEWEST_FIRST

    if intent == "current":

        def blended(c):
            sim = 1.0 - c.get("distance", 1.0)
            rec = recency_score(c.get("episode_date", ""), today, half_life_days)
            return (1 - CURRENT_RECENCY_WEIGHT) * sim + CURRENT_RECENCY_WEIGHT * rec

        ranked = sorted(cands, key=blended, reverse=True)
        return ranked[:top_k], NEWEST_FIRST

    if intent == "evolution":
        # Spread across episodes to cover the timeline: keep the most-relevant
        # few per episode, then the most-relevant overall up to top_k.
        per_ep: dict = {}
        for c in cands:  # already similarity-sorted
            key = c.get("episode_number") or c.get("episode_date")
            per_ep.setdefault(key, [])
            if len(per_ep[key]) < MAX_PER_EPISODE_EVOLUTION:
                per_ep[key].append(c)
        spread = [c for items in per_ep.values() for c in items]
        picked = _by_similarity(spread)[:top_k]
        return _by_date(picked, reverse=False), CHRONOLOGICAL

    if intent == "prediction":
        # No recency decay: the original (old) speculation must survive alongside
        # later updates; present chronologically (prediction -> outcome).
        return _by_date(cands[:top_k], reverse=False), CHRONOLOGICAL

    # historical / timeless / None -> pure relevance, newest-first presentation.
    return cands[:top_k], NEWEST_FIRST
