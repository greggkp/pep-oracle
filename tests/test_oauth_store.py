import boto3
import pytest
from moto import mock_aws

from pep_oracle import oauth_store


@pytest.fixture(params=["sqlite", "dynamodb"])
def store(request):
    if request.param == "sqlite":
        yield oauth_store.SqliteStore(":memory:")
        return
    with mock_aws():
        boto3.client("dynamodb", region_name="ap-southeast-2")  # ensure region in moto
        s = oauth_store.DynamoDbStore("test-oauth", region="ap-southeast-2")
        s.ensure_table()
        yield s


def test_client_roundtrip(store):
    created = store.put_client("c1", "My App", ["https://app.example/cb"])
    assert isinstance(created, int)
    rec = store.get_client("c1")
    assert rec is not None
    assert rec.client_id == "c1"
    assert rec.client_name == "My App"
    assert rec.redirect_uris == ["https://app.example/cb"]
    assert rec.created_at == created
    assert store.get_client("missing") is None


def test_auth_code_single_use(store):
    store.put_auth_code(
        "abc", client_id="c1", code_challenge="chal", redirect_uri="https://app/cb", ttl_seconds=60
    )
    rec = store.pop_auth_code("abc")
    assert rec is not None
    assert rec.client_id == "c1"
    assert rec.code_challenge == "chal"
    assert rec.redirect_uri == "https://app/cb"
    # single use — second pop is None
    assert store.pop_auth_code("abc") is None


def test_auth_code_expired_returns_none(store):
    store.put_auth_code(
        "old", client_id="c1", code_challenge="x", redirect_uri="https://app/cb", ttl_seconds=-1
    )
    assert store.pop_auth_code("old") is None


def test_refresh_roundtrip_and_revoke(store):
    store.put_refresh("t1", client_id="c1", family_id="f1", ttl_seconds=3600)
    rec = store.get_refresh("t1")
    assert rec is not None and rec.client_id == "c1" and rec.family_id == "f1"
    assert rec.revoked is False
    # conditional revoke: first call wins, second loses
    assert store.revoke_refresh("t1") is True
    assert store.revoke_refresh("t1") is False
    assert store.get_refresh("t1").revoked is True


def test_revoke_family_revokes_all_members(store):
    store.put_refresh("a", client_id="c1", family_id="fam", ttl_seconds=3600)
    store.put_refresh("b", client_id="c1", family_id="fam", ttl_seconds=3600)
    store.put_refresh("other", client_id="c1", family_id="zzz", ttl_seconds=3600)
    store.revoke_family("fam")
    assert store.get_refresh("a").revoked is True
    assert store.get_refresh("b").revoked is True
    assert store.get_refresh("other").revoked is False


def test_revoke_missing_token_returns_false(store):
    assert store.revoke_refresh("nope") is False


def test_revoke_family_empty_is_noop(store):
    store.put_refresh("x", client_id="c1", family_id="", ttl_seconds=3600)
    store.revoke_family("")
    assert store.get_refresh("x").revoked is False


def test_concurrent_revoke_exactly_one_wins(store):
    """Two threads racing to rotate the same refresh token: the conditional
    revoke must let exactly ONE win (the rotation), the other loses cleanly."""
    import threading

    store.put_refresh("race", client_id="c1", family_id="f1", ttl_seconds=3600)
    results = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()  # maximize contention
        results.append(store.revoke_refresh("race"))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True]  # exactly one winner
    assert store.get_refresh("race").revoked is True
