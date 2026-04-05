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

    from pep_oracle.server import app

    with TestClient(app) as tc:
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
    resp = client.get("/episodes")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    assert data[0]["episode_number"] == 1
    assert data[0]["ingested"] is False


def test_episodes_reflects_ingestion(client_and_collection):
    client, collection = client_and_collection
    _ingest_chunk(collection, "guid-2", 2)

    resp = client.get("/episodes")
    data = resp.json()
    by_num = {ep["episode_number"]: ep for ep in data}
    assert by_num[1]["ingested"] is False
    assert by_num[2]["ingested"] is True
    assert by_num[3]["ingested"] is False


def test_status_counts(client_and_collection):
    client, collection = client_and_collection
    _ingest_chunk(collection, "guid-1", 1)

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

    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ingested_count"] == 0
    assert data["earliest_date"] is None
    assert data["latest_date"] is None
    assert data["earliest_episode"] is None
    assert data["latest_episode"] is None


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


def test_topics_returns_extracted_topics(client_and_collection):
    client, _ = client_and_collection
    mock_topics = [
        {"topic": "Tariffs", "question": "What about tariffs?", "episode_number": 3},
        {"topic": "Immigration", "question": "What about immigration?", "episode_number": 1},
    ]
    with patch("pep_oracle.server.extract_topics", return_value=mock_topics):
        resp = client.get("/topics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["topics"] == mock_topics


def test_topics_returns_empty_on_failure(client_and_collection):
    client, _ = client_and_collection
    with patch("pep_oracle.server.extract_topics", return_value=[]):
        resp = client.get("/topics")
    assert resp.status_code == 200
    assert resp.json()["topics"] == []


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
