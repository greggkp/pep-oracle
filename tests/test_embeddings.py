import json

from pep_oracle import embeddings
from pep_oracle.embeddings import embed_texts


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


def test_embed_texts_calls_bedrock_and_returns_expected_shape(monkeypatch):
    """embed_texts delegates to _embed_one_bedrock for each text."""
    fake = _FakeBedrock()
    monkeypatch.setattr(embeddings, "_bedrock_client", lambda: fake)

    out = embed_texts(["hello", "hello world"])

    assert len(out) == 2
    assert len(out[0]) == 1024
    assert out[0] != out[1]  # distinct inputs -> distinct vectors
    # Titan v2 invoke body: inputText + dimensions + normalize
    assert fake.calls[0]["modelId"] == embeddings.config.EMBED_MODEL
    assert fake.calls[0]["body"] == {"inputText": "hello", "dimensions": 1024, "normalize": True}


def test_bedrock_retries_on_throttling(monkeypatch):
    attempts = {"n": 0}

    class _Throttler:
        def invoke_model(self, *, modelId, body):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise embeddings._ThrottlingError("slow down")
            return {"body": _FakeBody({"embedding": [0.1] * 1024})}

    monkeypatch.setattr(embeddings, "_bedrock_client", lambda: _Throttler())
    monkeypatch.setattr(embeddings.time, "sleep", lambda _s: None)  # no real backoff wait

    out = embed_texts(["x"])

    assert attempts["n"] == 3  # two failures, third succeeds
    assert len(out[0]) == 1024
