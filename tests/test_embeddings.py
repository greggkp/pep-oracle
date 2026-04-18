from pep_oracle.embeddings import embed_texts


def test_embed_texts_returns_expected_shape_and_distinct_vectors():
    """Integration test: loads bge-large-en-v1.5 (≈1.3 GB on first run, cached).

    First run downloads the model into ~/.cache/fastembed and may take
    10-30s. Subsequent runs are fast (sub-second).
    """
    result = embed_texts(["hello world", "goodbye world"])

    assert len(result) == 2
    assert len(result[0]) == 1024
    assert len(result[1]) == 1024
    # Non-zero embeddings
    assert any(v != 0.0 for v in result[0])
    assert any(v != 0.0 for v in result[1])
    # Distinct inputs produce distinct embeddings
    assert result[0] != result[1]
