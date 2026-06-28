from datetime import date

from pep_oracle.temporal import (
    CHRONOLOGICAL,
    NEWEST_FIRST,
    recency_score,
    select_for_intent,
)

TODAY = date(2026, 5, 30)


def _c(distance, ep_date, ep_num):
    return {"distance": distance, "episode_date": ep_date, "episode_number": ep_num}


def test_recency_score_decay():
    assert recency_score("2026-05-30", TODAY) == 1.0  # today
    assert abs(recency_score("2026-05-09", TODAY) - 0.5) < 1e-6  # one half-life (21d)
    assert recency_score("2024-01-01", TODAY) < 0.001  # very old -> ~0
    assert recency_score("not-a-date", TODAY) == 0.0
    assert recency_score("2027-01-01", TODAY) == 1.0  # future clamps to 1


def test_current_blends_recency_so_recent_relevant_wins():
    cands = [
        _c(0.10, "2025-01-01", 200),  # very relevant but old
        _c(0.30, "2026-05-29", 263),  # slightly less relevant but ~today
    ]
    results, order = select_for_intent(cands, "current", top_k=2, today=TODAY)
    assert order == NEWEST_FIRST
    # recent chunk should rank first despite slightly higher distance
    assert results[0]["episode_number"] == 263


def test_prediction_keeps_old_relevant_and_orders_chronological():
    cands = [
        _c(0.10, "2025-06-01", 215),  # the original (old) prediction, most relevant
        _c(0.20, "2026-05-01", 258),  # later update
    ]
    results, order = select_for_intent(cands, "prediction", top_k=2, today=TODAY)
    assert order == CHRONOLOGICAL
    # no recency decay -> the old high-relevance prediction is retained
    nums = [r["episode_number"] for r in results]
    assert 215 in nums and 258 in nums


def test_evolution_spreads_across_episodes():
    cands = [
        _c(0.10, "2026-05-01", 258),
        _c(0.12, "2026-05-01", 258),  # 2nd from same ep
        _c(0.15, "2026-05-01", 258),  # 3rd from same ep -> dropped (cap 2/ep)
        _c(0.20, "2025-06-01", 215),
        _c(0.22, "2025-09-01", 230),
    ]
    results, order = select_for_intent(cands, "evolution", top_k=4, today=TODAY)
    assert order == CHRONOLOGICAL
    eps = [r["episode_number"] for r in results]
    assert eps == sorted(eps, reverse=False) or eps == sorted(set(eps))  # chronological-ish
    # multiple distinct episodes represented (timeline coverage)
    assert len(set(eps)) >= 2
    # at most 2 from episode 258
    assert eps.count(258) <= 2


def test_historical_and_default_are_relevance_newest_first():
    cands = [_c(0.10, "2025-01-01", 200), _c(0.20, "2026-05-01", 258)]
    for intent in ("historical", "timeless", None):
        results, order = select_for_intent(cands, intent, top_k=2, today=TODAY)
        assert order == NEWEST_FIRST
        # pure relevance: most similar first in the returned set
        assert results[0]["episode_number"] == 200


def test_select_handles_empty_and_small_pools():
    assert select_for_intent([], "current", top_k=5, today=TODAY) == ([], NEWEST_FIRST)
    one = [_c(0.1, "2026-05-01", 258)]
    res, _ = select_for_intent(one, "evolution", top_k=5, today=TODAY)
    assert len(res) == 1
