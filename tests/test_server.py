"""Tests for the FastAPI server endpoints using TestClient (no Playwright)."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
from fastapi.testclient import TestClient

from pep_oracle.models import Chunk, Episode
from pep_oracle.store import add_chunks, get_ingested_guids


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
