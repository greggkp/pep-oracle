"""Tests for the FastAPI server endpoints using TestClient (no Playwright)."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
from fastapi.testclient import TestClient

from pep_oracle.models import Chunk, Episode
from pep_oracle.store import add_chunks, get_ingested_guids, get_ingestion_stats


_counter = 0


def _make_episode(num, guid=None):
    return Episode(
        guid=guid or f"guid-{num}",
        title=f"Test Episode (Ep {num})",
        pub_date=datetime(2026, 1, num, tzinfo=timezone.utc),
        audio_url=f"https://example.com/ep{num}.mp3",
        description=f"Episode {num}",
        duration_seconds=3600,
        episode_number=num,
    )


EPISODES = [_make_episode(i) for i in range(1, 4)]


@pytest.fixture()
def client_and_collection():
    """TestClient backed by an in-memory ChromaDB and mocked feed."""
    global _counter
    _counter += 1
    chroma_client = chromadb.Client()
    collection = chroma_client.get_or_create_collection(
        name=f"server_test_{_counter}", metadata={"hnsw:space": "cosine"}
    )

    patches = [
        patch("pep_oracle.server.fetch_episodes", return_value=EPISODES),
        patch("pep_oracle.server._get_fresh_collection", return_value=collection),
        patch(
            "pep_oracle.server.get_ingested_guids",
            wraps=lambda col: get_ingested_guids(collection),
        ),
        patch(
            "pep_oracle.server.get_ingestion_stats",
            wraps=lambda col: get_ingestion_stats(collection),
        ),
        patch("pep_oracle.server.CHROMA_DIR", Path("/tmp/fake-chroma")),
    ]
    for p in patches:
        p.start()

    # Reset all caches so each test starts fresh (prevents cross-test contamination)
    from pep_oracle.server import _caches
    for entry in _caches.values():
        entry.data = None
        entry.updated_at = None

    from pep_oracle.server import app

    with TestClient(app) as tc:
        # Wait for lifespan background cache refreshes to complete
        import time
        for entry in _caches.values():
            for _ in range(50):
                if not entry.refreshing:
                    break
                time.sleep(0.05)
        yield tc, collection

    for p in patches:
        p.stop()


def _ingest_chunk(collection, guid, episode_number):
    chunk = Chunk(
        chunk_id=f"{guid}_0000",
        episode_guid=guid,
        text="Some transcript text",
        episode_title=f"Episode {episode_number}",
        episode_date="2026-01-01",
        episode_number=episode_number,
        start_time=0.0,
        end_time=60.0,
    )
    add_chunks(collection, [chunk], [[0.1] * 10])


def test_health(client_and_collection):
    client, _ = client_and_collection
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_episodes_returns_all(client_and_collection):
    client, _ = client_and_collection
    from pep_oracle.server import _caches, _fetch_episodes
    _caches["episodes"].set(_fetch_episodes())
    resp = client.get("/episodes")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["episodes"]) == 3
    assert data["episodes"][0]["episode_number"] == 1
    assert data["episodes"][0]["ingested"] is False


def test_episodes_reflects_ingestion(client_and_collection):
    client, collection = client_and_collection
    _ingest_chunk(collection, "guid-2", 2)

    from pep_oracle.server import _caches, _fetch_episodes
    _caches["episodes"].set(_fetch_episodes())
    resp = client.get("/episodes")
    data = resp.json()
    by_num = {ep["episode_number"]: ep for ep in data["episodes"]}
    assert by_num[1]["ingested"] is False
    assert by_num[2]["ingested"] is True
    assert by_num[3]["ingested"] is False


def test_episodes_includes_stale_field(client_and_collection):
    client, _ = client_and_collection
    resp = client.get("/episodes")
    assert resp.status_code == 200
    data = resp.json()
    assert "stale" in data
    assert "episodes" in data


def test_status_counts(client_and_collection):
    client, collection = client_and_collection
    _ingest_chunk(collection, "guid-1", 1)

    from pep_oracle.server import _caches, _fetch_status
    _caches["status"].set(_fetch_status())
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["feed_count"] == 3
    assert data["ingested_count"] == 1
    assert data["chunk_count"] == 1
    assert data["earliest_date"] == "2026-01-01"
    assert data["latest_date"] == "2026-01-01"
    assert data["earliest_episode"] == 1
    assert data["latest_episode"] == 1


def test_status_empty_collection(client_and_collection):
    """Status with no ingested episodes should return null for range fields."""
    client, _ = client_and_collection

    from pep_oracle.server import _caches, _fetch_status
    _caches["status"].set(_fetch_status())
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ingested_count"] == 0
    assert data["earliest_date"] is None
    assert data["latest_date"] is None
    assert data["earliest_episode"] is None
    assert data["latest_episode"] is None


def test_status_includes_stale_field(client_and_collection):
    client, _ = client_and_collection
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "stale" in data


def test_reload_get(client_and_collection):
    client, _ = client_and_collection
    resp = client.get("/reload")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_reload_post(client_and_collection):
    client, _ = client_and_collection
    resp = client.post("/reload")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_ask_returns_answer(client_and_collection):
    client, _ = client_and_collection
    with patch("pep_oracle.server.do_ask", return_value="Test answer"):
        resp = client.post("/ask", json={"question": "What is PEP?"})
    assert resp.status_code == 200
    assert resp.json()["answer"] == "Test answer"


def test_ingest_status_initially_idle(client_and_collection):
    client, _ = client_and_collection
    resp = client.get("/ingest/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is False


def test_root_returns_html(client_and_collection):
    client, _ = client_and_collection
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_topics_returns_file_based_topics(client_and_collection, tmp_path):
    client, _ = client_and_collection
    topics_path = tmp_path / "topics.json"
    import json
    topics_path.write_text(json.dumps({"episodes": [
        {"episode_number": 3, "date": "2026-01-03", "topics": ["Tariffs", "Immigration"]},
    ]}))
    from pep_oracle.server import _caches, _fetch_topics
    with patch("pep_oracle.server.TOPICS_PATH", topics_path):
        _caches["topics"].set(_fetch_topics())
    resp = client.get("/topics")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["episodes"]) == 1
    assert data["episodes"][0]["topics"] == ["Tariffs", "Immigration"]


def test_topics_returns_empty_when_no_file(client_and_collection, tmp_path):
    client, _ = client_and_collection
    topics_path = tmp_path / "nonexistent.json"
    from pep_oracle.server import _caches, _fetch_topics
    with patch("pep_oracle.server.TOPICS_PATH", topics_path):
        _caches["topics"].set(_fetch_topics())
    resp = client.get("/topics")
    assert resp.status_code == 200
    assert resp.json()["episodes"] == []


def test_topics_includes_stale_field(client_and_collection):
    client, _ = client_and_collection
    resp = client.get("/topics")
    assert resp.status_code == 200
    data = resp.json()
    assert "stale" in data


def test_ask_passes_history_to_do_ask(client_and_collection):
    client, _ = client_and_collection
    history = [
        {"role": "user", "content": "What about tariffs?"},
        {"role": "assistant", "content": "They discussed tariffs in Ep 255..."},
    ]
    with patch("pep_oracle.server.do_ask", return_value="Follow-up answer") as mock_ask:
        resp = client.post("/ask", json={
            "question": "What about the EU?",
            "history": history,
        })
    assert resp.status_code == 200
    assert resp.json()["answer"] == "Follow-up answer"
    mock_ask.assert_called_once_with(
        "What about the EU?", top_k=10, history=history,
    )


def test_ask_without_history_passes_empty_list(client_and_collection):
    client, _ = client_and_collection
    with patch("pep_oracle.server.do_ask", return_value="Answer") as mock_ask:
        resp = client.post("/ask", json={"question": "What is PEP?"})
    assert resp.status_code == 200
    mock_ask.assert_called_once_with("What is PEP?", top_k=10, history=[])


def test_freshness_returns_all_caches(client_and_collection):
    client, _ = client_and_collection
    resp = client.get("/freshness")
    assert resp.status_code == 200
    data = resp.json()
    assert "topics" in data
    assert "status" in data
    assert "episodes" in data
    for key in ("topics", "status", "episodes"):
        assert "stale" in data[key]
        assert "updated_at" in data[key]


def test_ingest_parses_episode_input(client_and_collection):
    """POST /ingest with episode_input spawns the worker with --episode args."""
    import json as _json
    import time
    client, _collection = client_and_collection

    class _FakeStdout:
        def __init__(self, lines):
            self._lines = list(lines)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._lines:
                raise StopAsyncIteration
            return self._lines.pop(0)

    class _FakeProc:
        def __init__(self, lines):
            self.stdout = _FakeStdout(lines)

        async def wait(self):
            return 0

    captured: dict = {}

    async def _fake_exec(*args, **kwargs):
        captured["cmd"] = args
        payload = _json.dumps({"processed": 0, "skipped": 3, "failed": 0}).encode()
        return _FakeProc([b"RESULT: " + payload + b"\n"])

    with patch("pep_oracle.server.asyncio.create_subprocess_exec", side_effect=_fake_exec):
        resp = client.post("/ingest", json={"episode_input": "1-2"})
        assert resp.status_code == 200
        for _ in range(50):
            if "cmd" in captured:
                break
            time.sleep(0.1)

    assert "cmd" in captured, "ingest worker was never spawned"
    cmd = captured["cmd"]
    episodes_passed = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--episode"]
    assert sorted(episodes_passed) == ["1", "2"]


def test_ingest_invalid_episode_input(client_and_collection):
    """POST /ingest with invalid episode_input returns 400."""
    client, _ = client_and_collection
    resp = client.post("/ingest", json={"episode_input": "abc"})
    assert resp.status_code == 400
    assert "Invalid" in resp.json()["detail"]


def test_topics_returns_all_uningested_episodes(client_and_collection, tmp_path):
    """The /topics endpoint should return ALL uningested episodes, not just newer-than-latest."""
    client, collection = client_and_collection

    # Ingest only episode 2 (creates a gap: 1 is older and uningested)
    _ingest_chunk(collection, "guid-2", 2)

    topics_path = tmp_path / "topics.json"
    import json
    topics_path.write_text(json.dumps({"episodes": []}))

    from pep_oracle.server import _caches, _fetch_topics
    with patch("pep_oracle.server.TOPICS_PATH", topics_path):
        _caches["topics"].set(_fetch_topics())

    resp = client.get("/topics")
    data = resp.json()
    not_ingested = data["not_ingested_episodes"]

    # Episode 1 is older than 2 but should still appear; episode 3 is newer
    assert 1 in not_ingested
    assert 3 in not_ingested
    assert 2 not in not_ingested


def test_lambda_handler_is_constructed():
    """server.handler is a Mangum ASGI adapter wrapping the FastAPI app, so the
    same app runs under uvicorn locally and Lambda in prod."""
    from pep_oracle import server

    assert server.handler is not None
    assert server.handler.__class__.__name__ == "Mangum"


def test_version_reports_code_only_by_default(monkeypatch):
    from fastapi.testclient import TestClient
    from pep_oracle import config, server

    monkeypatch.setattr(config, "SERVE_FROM_ARTIFACT", False)
    monkeypatch.setattr(config, "GIT_SHA", "abc1234")
    with TestClient(server.app) as client:
        r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert body["code_git_sha"] == "abc1234"
    assert "code_semver" in body
    assert "corpus_version" not in body  # artifact serving off


def test_version_reports_corpus_when_serving_from_artifact(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from pep_oracle import config, corpus, server

    corpus.write_artifact(
        [{"chunk_id": "a", "text": "x", "embedding": [1.0, 0.0],
          "metadata": {"episode_number": 251, "episode_date": "2026-04-01",
                       "episode_guid": "g", "episode_title": "t",
                       "start_time": 0.0, "end_time": 1.0}}],
        dest=str(tmp_path), version="v0042",
        embed_model="amazon.titan-embed-text-v2:0", dims=2, git_sha="s",
        built_at="2026-06-01T06:14:00+00:00",
    )
    monkeypatch.setattr(config, "SERVE_FROM_ARTIFACT", True)
    monkeypatch.setattr(config, "CORPUS_URI", str(tmp_path))
    with TestClient(server.app) as client:
        r = client.get("/version")
    body = r.json()
    assert body["corpus_version"] == "v0042"
    assert body["corpus_episode_range"] == [251, 251]
    assert body["embed_model"] == "amazon.titan-embed-text-v2:0"
    assert body["corpus_built_at"] == "2026-06-01T06:14:00+00:00"


def test_version_corpus_error_is_generic_and_leaks_no_path(tmp_path, monkeypatch):
    """/version is public (not behind the /mcp bearer gate), so a corpus load
    failure must return a generic marker, not the raw exception (which would leak
    the corpus path / S3 bucket)."""
    from fastapi.testclient import TestClient
    from pep_oracle import config, server

    secret_path = str(tmp_path / "nonexistent-secret-bucket-name")
    monkeypatch.setattr(config, "SERVE_FROM_ARTIFACT", True)
    monkeypatch.setattr(config, "CORPUS_URI", secret_path)  # no current.json -> load fails
    with TestClient(server.app) as client:
        r = client.get("/version")
    assert r.status_code == 200  # never 500s
    assert r.json()["corpus_error"] == "corpus manifest unavailable"
    assert "nonexistent-secret-bucket-name" not in r.text  # internal path not leaked


def test_mount_builds_oauth_store_from_config(tmp_path, monkeypatch):
    """mount_mcp_if_configured builds an OAuthStore from config and passes the
    STORE OBJECT (not a db-path string) to register_oauth_routes."""
    from fastapi import FastAPI

    from pep_oracle import config, oauth_store, server

    captured = {}

    class _Stop(Exception):
        pass

    def fake_register(app, signing_key, public_url, store):
        captured["store"] = store
        raise _Stop  # short-circuit before the (heavy) MCP mount that follows

    monkeypatch.setenv("PEP_ORACLE_PUBLIC_URL", "https://pep-oracle.example")
    monkeypatch.setenv("PEP_ORACLE_OAUTH_TRUSTS_UPSTREAM_AUTH", "1")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)          # don't touch ~/.pep-oracle
    monkeypatch.setattr(config, "OAUTH_STORE", "sqlite")
    monkeypatch.setattr(server.oauth, "register_oauth_routes", fake_register)
    monkeypatch.setattr(server, "_resolve_signing_key", lambda: "k")

    try:
        server.mount_mcp_if_configured(FastAPI())
    except _Stop:
        pass

    store = captured["store"]
    # a store object, NOT a path string:
    assert not isinstance(store, str)
    assert hasattr(store, "get_refresh") and hasattr(store, "revoke_refresh")
    assert isinstance(store, oauth_store.SqliteStore)
