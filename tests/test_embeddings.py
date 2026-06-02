import json

from pep_oracle import embeddings
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


class _FakeBody:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()


class _FakeBedrock:
    """Records invoke_model calls and returns a deterministic embedding."""

    def __init__(self):
        self.calls = []

    def invoke_model(self, *, modelId, body):
        parsed = json.loads(body)
        self.calls.append({"modelId": modelId, "body": parsed})
        text = parsed["inputText"]
        # 1024-d vector seeded by text length so distinct inputs differ.
        return {"body": _FakeBody({"embedding": [float(len(text))] * 1024})}


def test_bedrock_backend_calls_invoke_model_with_titan_body(monkeypatch):
    fake = _FakeBedrock()
    monkeypatch.setattr(embeddings, "_bedrock_client", lambda: fake)
    monkeypatch.setattr(embeddings.config, "EMBED_BACKEND", "bedrock")

    out = embeddings.embed_texts(["hello", "hello world"])

    assert len(out) == 2
    assert len(out[0]) == 1024
    assert out[0] != out[1]  # distinct inputs -> distinct vectors
    # Titan v2 invoke body: inputText + dimensions + normalize
    assert fake.calls[0]["modelId"] == embeddings.config.EMBED_MODEL
    assert fake.calls[0]["body"] == {"inputText": "hello", "dimensions": 1024, "normalize": True}


def test_bedrock_backend_retries_on_throttling(monkeypatch):
    attempts = {"n": 0}

    class _Throttler:
        def invoke_model(self, *, modelId, body):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise embeddings._ThrottlingError("slow down")
            return {"body": _FakeBody({"embedding": [0.1] * 1024})}

    monkeypatch.setattr(embeddings, "_bedrock_client", lambda: _Throttler())
    monkeypatch.setattr(embeddings.config, "EMBED_BACKEND", "bedrock")
    monkeypatch.setattr(embeddings.time, "sleep", lambda _s: None)  # no real backoff wait

    out = embeddings.embed_texts(["x"])

    assert attempts["n"] == 3  # two failures, third succeeds
    assert len(out[0]) == 1024
