"""Tests for the /topics endpoint — deterministic file-based topics."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
from fastapi.testclient import TestClient

from pep_oracle.models import Chunk, Episode
from pep_oracle.store import add_chunks


def _make_episode(num, description="Episode description"):
    return Episode(
        guid=f"guid-{num}",
        title=f"Test Episode (Ep {num})",
        pub_date=datetime(2026, 1, num, tzinfo=timezone.utc),
        audio_url=f"https://example.com/ep{num}.mp3",
        description=description,
        duration_seconds=3600,
        episode_number=num,
    )


FEED_EPISODES = [_make_episode(i) for i in range(1, 6)]

TOPICS_DATA = {
    "episodes": [
        {"episode_number": 5, "date": "2026-01-05", "topics": ["Cuba", "Iran Latest"]},
        {"episode_number": 4, "date": "2026-01-04", "topics": ["Hegseth Issues"]},
        {"episode_number": 3, "date": "2026-01-03", "topics": ["Ukraine Corner"]},
    ]
}


def _ingest(collection, episode_number):
    chunk = Chunk(
        chunk_id=f"guid-{episode_number}_0000",
        episode_guid=f"guid-{episode_number}",
        text="Transcript text",
        episode_title=f"Test Episode (Ep {episode_number})",
        episode_date=f"2026-01-{episode_number:02d}",
        episode_number=episode_number,
        start_time=0.0,
        end_time=60.0,
    )
    add_chunks(collection, [chunk], [[0.1] * 10])


@pytest.fixture()
def client_and_collection(tmp_path):
    """FastAPI TestClient with mocked feed, topics file, and in-memory ChromaDB."""
    chroma_client = chromadb.Client()
    collection = chroma_client.get_or_create_collection(
        name="pep_oracle", metadata={"hnsw:space": "cosine"}
    )

    topics_path = tmp_path / "topics.json"
    topics_path.write_text(json.dumps(TOPICS_DATA))

    with (
        patch("pep_oracle.server.fetch_episodes", return_value=FEED_EPISODES),
        patch("pep_oracle.server._get_fresh_collection", return_value=collection),
        patch("pep_oracle.server.TOPICS_PATH", topics_path),
    ):
        from pep_oracle.server import _caches
        for entry in _caches.values():
            entry.data = None
            entry.updated_at = None

        from pep_oracle.server import app
        yield TestClient(app), collection, topics_path

    chroma_client.delete_collection("pep_oracle")


def _populate_topics_cache():
    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())


def test_topics_response_has_episodes_key(client_and_collection):
    """The /topics response has 'episodes' (not 'topics' and 'pool')."""
    client, _, _ = client_and_collection
    _populate_topics_cache()
    resp = client.get("/topics")
    assert resp.status_code == 200
    data = resp.json()
    assert "episodes" in data
    assert "not_ingested_episodes" in data
    assert "topics" not in data
    assert "pool" not in data


def test_topics_episodes_match_file(client_and_collection):
    """Episodes in response match what's in topics.json."""
    client, _, _ = client_and_collection
    _populate_topics_cache()
    resp = client.get("/topics")
    data = resp.json()
    assert len(data["episodes"]) == 3
    assert data["episodes"][0]["episode_number"] == 5
    assert "Cuba" in data["episodes"][0]["topics"]


def test_topics_not_ingested_episodes(client_and_collection):
    """Not-ingested episodes are correctly detected."""
    client, _, _ = client_and_collection
    _populate_topics_cache()
    resp = client.get("/topics")
    data = resp.json()
    assert sorted(data["not_ingested_episodes"]) == [1, 2, 3, 4, 5]


def test_topics_some_ingested(client_and_collection):
    """With highest ingested ep=5, older gaps are excluded."""
    client, collection, _ = client_and_collection
    _ingest(collection, 1)
    _ingest(collection, 3)
    _ingest(collection, 5)
    _populate_topics_cache()
    resp = client.get("/topics")
    data = resp.json()
    assert data["not_ingested_episodes"] == []


def test_topics_only_newer_flagged(client_and_collection):
    """Only episodes newer than the highest ingested are flagged."""
    client, collection, _ = client_and_collection
    _ingest(collection, 1)
    _ingest(collection, 2)
    _ingest(collection, 3)
    _populate_topics_cache()
    resp = client.get("/topics")
    data = resp.json()
    assert data["not_ingested_episodes"] == [4, 5]


def test_topics_all_ingested(client_and_collection):
    """With all episodes ingested, not_ingested_episodes is empty."""
    client, collection, _ = client_and_collection
    for i in range(1, 6):
        _ingest(collection, i)
    _populate_topics_cache()
    resp = client.get("/topics")
    data = resp.json()
    assert data["not_ingested_episodes"] == []


def test_topics_bootstrap_when_file_missing(tmp_path):
    """When topics.json doesn't exist, bootstrap from feed episodes."""
    chroma_client = chromadb.Client()
    collection = chroma_client.get_or_create_collection(
        name="pep_oracle_boot", metadata={"hnsw:space": "cosine"}
    )
    topics_path = tmp_path / "topics.json"
    # Do NOT create the file — it should be bootstrapped

    feed_eps = [
        _make_episode(3, (
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Dr Dave<br />"
            "1:06:30 - Cuba<br />"
            "1:23:04 - Iran Latest</p>"
        )),
        _make_episode(2, (
            "<p>Timestamps:<br />"
            "0:00 - Introducing: Host<br />"
            "10:00 - Hegseth Issues</p>"
        )),
    ]

    with (
        patch("pep_oracle.server.fetch_episodes", return_value=feed_eps),
        patch("pep_oracle.server._get_fresh_collection", return_value=collection),
        patch("pep_oracle.server.TOPICS_PATH", topics_path),
    ):
        from pep_oracle.server import _caches, _fetch_topics, app
        _caches["topics"].data = None
        _caches["topics"].updated_at = None
        _caches["topics"].set(_fetch_topics())

        client = TestClient(app)
        resp = client.get("/topics")
        data = resp.json()
        assert len(data["episodes"]) == 2
        assert data["episodes"][0]["episode_number"] == 3
        assert "Cuba" in data["episodes"][0]["topics"]

    chroma_client.delete_collection("pep_oracle_boot")


def test_topics_not_ingested_are_ints(client_and_collection):
    """Episode numbers in not_ingested_episodes must be ints."""
    client, _, _ = client_and_collection
    _populate_topics_cache()
    resp = client.get("/topics")
    data = resp.json()
    for ep_num in data["not_ingested_episodes"]:
        assert isinstance(ep_num, int)


def test_topics_stale_flag(client_and_collection):
    """Response includes stale flag."""
    client, _, _ = client_and_collection
    _populate_topics_cache()
    resp = client.get("/topics")
    data = resp.json()
    assert "stale" in data
