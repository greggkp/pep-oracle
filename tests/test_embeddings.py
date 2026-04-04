from unittest.mock import MagicMock, call

from pep_oracle.embeddings import BATCH_SIZE, embed_texts


def _mock_embedding(index: int, dims: int = 4) -> MagicMock:
    emb = MagicMock()
    emb.index = index
    emb.embedding = [float(index)] * dims
    return emb


def _mock_client(batch_responses: list[list[MagicMock]]) -> MagicMock:
    client = MagicMock()
    responses = []
    for batch in batch_responses:
        resp = MagicMock()
        resp.data = batch
        responses.append(resp)
    client.embeddings.create.side_effect = responses
    return client


def test_single_batch():
    texts = ["hello", "world"]
    client = _mock_client([[_mock_embedding(0), _mock_embedding(1)]])
    result = embed_texts(texts, client=client)
    assert len(result) == 2
    assert result[0] == [0.0, 0.0, 0.0, 0.0]
    assert result[1] == [1.0, 1.0, 1.0, 1.0]
    client.embeddings.create.assert_called_once()


def test_multiple_batches():
    texts = [f"text_{i}" for i in range(BATCH_SIZE + 5)]
    batch1 = [_mock_embedding(i) for i in range(BATCH_SIZE)]
    batch2 = [_mock_embedding(i) for i in range(5)]
    client = _mock_client([batch1, batch2])

    result = embed_texts(texts, client=client)
    assert len(result) == BATCH_SIZE + 5
    assert client.embeddings.create.call_count == 2


def test_respects_response_ordering():
    """Embeddings should be sorted by index even if API returns out of order."""
    texts = ["a", "b", "c"]
    # Return in reverse order
    client = _mock_client([[_mock_embedding(2), _mock_embedding(0), _mock_embedding(1)]])
    result = embed_texts(texts, client=client)
    assert result[0] == [0.0, 0.0, 0.0, 0.0]
    assert result[1] == [1.0, 1.0, 1.0, 1.0]
    assert result[2] == [2.0, 2.0, 2.0, 2.0]
