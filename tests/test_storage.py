import pep_oracle._storage as storage


def test_local_roundtrip_bytes_and_text(tmp_path):
    p = tmp_path / "sub" / "blob.bin"
    storage.put_bytes(str(p), b"\x00\x01\x02")
    assert storage.get_bytes(str(p)) == b"\x00\x01\x02"  # parent dir auto-created

    t = tmp_path / "sub" / "doc.json"
    storage.put_text(str(t), '{"a": 1}')
    assert storage.get_text(str(t)) == '{"a": 1}'


def test_is_s3():
    assert storage.is_s3("s3://bucket/key")
    assert not storage.is_s3("/local/path")


class _FakeS3:
    def __init__(self):
        self.store = {}

    def put_object(self, *, Bucket, Key, Body):
        self.store[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):
        data = self.store[(Bucket, Key)]

        class _Body:
            def read(self_inner):
                return data

        return {"Body": _Body()}


def test_s3_roundtrip(monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(storage, "_s3", lambda: fake)

    storage.put_bytes("s3://corpus/corpus/v0001.parquet", b"PARQUET")
    assert ("corpus", "corpus/v0001.parquet") in fake.store
    assert storage.get_bytes("s3://corpus/corpus/v0001.parquet") == b"PARQUET"
