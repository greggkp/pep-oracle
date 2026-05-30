from pep_oracle.eval_retrieval import (
    aggregate,
    recall_at_k,
    reciprocal_rank,
    resolve_relevant_episodes,
)


def test_recall_at_k():
    # relevant episode 7 is at position 3 (rank 3)
    assert recall_at_k([5, 3, 7, 1], {7}, k=2) == 0  # not in first 2
    assert recall_at_k([5, 3, 7, 1], {7}, k=3) == 1  # in first 3
    assert recall_at_k([5, 3, 7, 1], {9}, k=4) == 0  # absent
    assert recall_at_k([7, 7, 7], {7}, k=1) == 1     # duplicates fine


def test_reciprocal_rank():
    assert reciprocal_rank([5, 7, 3], {7}) == 0.5     # first relevant at rank 2
    assert reciprocal_rank([7, 5, 3], {7}) == 1.0     # rank 1
    assert reciprocal_rank([1, 2, 3], {9}) == 0.0     # absent
    assert reciprocal_rank([5, 7, 7], {7}) == 0.5     # first occurrence only


def test_resolve_relevant_episodes():
    docs = ["alpha the byrd rule beta", "gamma", "byrd rule again"]
    metas = [{"episode_number": 262}, {"episode_number": 200}, {"episode_number": 262}]
    assert resolve_relevant_episodes(docs, metas, "byrd rule") == {262}
    assert resolve_relevant_episodes(docs, metas, "missing") == set()


def test_aggregate_means():
    # two cases: case A recall@5 hit, RR 1.0; case B miss, RR 0.0
    per_case = [
        {"recall": {5: 1, 10: 1}, "rr": 1.0, "type": "specific_term"},
        {"recall": {5: 0, 10: 1}, "rr": 0.5, "type": "topic_paraphrase"},
    ]
    agg = aggregate(per_case, ks=[5, 10])
    assert agg["recall"][5] == 0.5
    assert agg["recall"][10] == 1.0
    assert agg["mrr"] == 0.75
