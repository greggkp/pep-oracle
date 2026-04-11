"""Tests for the /topics endpoint's not_ingested_episodes detection."""

from datetime import datetime, timezone
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


def _make_episode_no_number(guid, title, day):
    """Episode whose title doesn't match the episode number regex."""
    return Episode(
        guid=guid,
        title=title,
        pub_date=datetime(2026, 1, day, tzinfo=timezone.utc),
        audio_url="https://example.com/bonus.mp3",
        description="Bonus content",
        duration_seconds=1800,
        episode_number=None,
    )


FEED_EPISODES = [_make_episode(i) for i in range(1, 6)]

MOCK_TOPICS = [
    {"topic": "Topic A", "question": "What about A?", "episode_number": 5},
    {"topic": "Topic B", "question": "What about B?", "episode_number": 4},
]


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
def client_and_collection():
    """FastAPI TestClient with mocked feed, topics, and in-memory ChromaDB."""
    chroma_client = chromadb.Client()
    collection = chroma_client.get_or_create_collection(
        name="pep_oracle", metadata={"hnsw:space": "cosine"}
    )

    with (
        patch("pep_oracle.server.fetch_episodes", return_value=FEED_EPISODES),
        patch("pep_oracle.server.extract_topics", return_value=MOCK_TOPICS),
        patch("pep_oracle.server._get_fresh_collection", return_value=collection),
    ):
        from pep_oracle.server import app

        yield TestClient(app), collection

    # Clean up: delete collection to avoid polluting other test files
    # (chromadb.Client() shares state via SharedSystemClient cache)
    chroma_client.delete_collection("pep_oracle")


def test_topics_response_contains_not_ingested_key(client_and_collection):
    """The /topics response must include the not_ingested_episodes field."""
    client, _ = client_and_collection
    resp = client.get("/topics")
    assert resp.status_code == 200
    data = resp.json()
    assert "not_ingested_episodes" in data
    assert "topics" in data


def test_topics_no_episodes_ingested(client_and_collection):
    """With no episodes ingested, all feed episode numbers appear in not_ingested_episodes."""
    client, _ = client_and_collection
    resp = client.get("/topics")
    data = resp.json()
    assert sorted(data["not_ingested_episodes"]) == [1, 2, 3, 4, 5]


def test_topics_some_episodes_ingested(client_and_collection):
    """With some episodes ingested, only un-ingested ones appear."""
    client, collection = client_and_collection
    _ingest(collection, 1)
    _ingest(collection, 3)
    _ingest(collection, 5)
    resp = client.get("/topics")
    data = resp.json()
    assert sorted(data["not_ingested_episodes"]) == [2, 4]


def test_topics_all_episodes_ingested(client_and_collection):
    """With all episodes ingested, not_ingested_episodes is empty."""
    client, collection = client_and_collection
    for i in range(1, 6):
        _ingest(collection, i)
    resp = client.get("/topics")
    data = resp.json()
    assert data["not_ingested_episodes"] == []


def test_topics_chromadb_failure_returns_all_as_not_ingested():
    """If ChromaDB fails, all feed episodes are treated as not-ingested."""
    def _broken_collection():
        raise RuntimeError("ChromaDB unavailable")

    with (
        patch("pep_oracle.server.fetch_episodes", return_value=FEED_EPISODES),
        patch("pep_oracle.server.extract_topics", return_value=MOCK_TOPICS),
        patch("pep_oracle.server._get_fresh_collection", side_effect=_broken_collection),
    ):
        from pep_oracle.server import app

        client = TestClient(app)
        resp = client.get("/topics")
        data = resp.json()
        assert sorted(data["not_ingested_episodes"]) == [1, 2, 3, 4, 5]


def test_topics_not_ingested_episodes_are_ints(client_and_collection):
    """Episode numbers in not_ingested_episodes must be ints, not strings."""
    client, _ = client_and_collection
    resp = client.get("/topics")
    data = resp.json()
    for ep_num in data["not_ingested_episodes"]:
        assert isinstance(ep_num, int)


def test_topics_episodes_without_number_excluded():
    """Episodes with episode_number=None are excluded from not_ingested_episodes."""
    episodes_with_bonus = FEED_EPISODES + [
        _make_episode_no_number("guid-bonus", "Bonus Episode", 10),
    ]
    with (
        patch("pep_oracle.server.fetch_episodes", return_value=episodes_with_bonus),
        patch("pep_oracle.server.extract_topics", return_value=MOCK_TOPICS),
        patch("pep_oracle.server._get_fresh_collection") as mock_col,
    ):
        mock_col.return_value.get.return_value = {"metadatas": []}
        from pep_oracle.server import app

        client = TestClient(app)
        resp = client.get("/topics")
        data = resp.json()
        # Should have episodes 1-5 but NOT the bonus (None episode_number)
        assert sorted(data["not_ingested_episodes"]) == [1, 2, 3, 4, 5]
