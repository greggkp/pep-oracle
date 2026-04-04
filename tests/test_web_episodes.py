"""Test that the web UI correctly reflects ingestion status.

Uses Playwright against the real FastAPI app with an in-memory ChromaDB
collection and a mocked RSS feed.
"""

import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
import uvicorn

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from pep_oracle.models import Chunk, Episode
from pep_oracle.store import add_chunks, get_ingested_guids, get_ingestion_stats


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


EPISODES = [_make_episode(i) for i in range(1, 6)]


def _ingest_into_collection(collection, guid: str, episode_number: int):
    chunk = Chunk(
        chunk_id=f"{guid}_0000",
        episode_guid=guid,
        text="Some transcript text",
        episode_title=f"Episode {episode_number}",
        episode_date=f"2026-01-{episode_number:02d}",
        episode_number=episode_number,
        start_time=0.0,
        end_time=60.0,
    )
    add_chunks(collection, [chunk], [[0.1] * 10])


@pytest.fixture()
def server_with_collection():
    """Start the real FastAPI app with an in-memory ChromaDB and mocked feed."""
    client = chromadb.Client()
    collection = client.get_or_create_collection(
        name="pep_oracle", metadata={"hnsw:space": "cosine"}
    )

    patches = [
        patch("pep_oracle.server.fetch_episodes", return_value=EPISODES),
        patch("pep_oracle.server._get_fresh_collection", return_value=collection),
        patch("pep_oracle.server.get_ingested_guids", wraps=lambda col: get_ingested_guids(collection)),
        patch("pep_oracle.server.get_ingestion_stats", wraps=lambda col: get_ingestion_stats(collection)),
        patch("pep_oracle.server.CHROMA_DIR", Path("/tmp/fake-chroma")),
    ]
    for p in patches:
        p.start()

    from pep_oracle.server import app

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)

    started = threading.Event()
    original_startup = server.startup

    async def _startup_then_signal(*a, **kw):
        await original_startup(*a, **kw)
        started.set()

    server.startup = _startup_then_signal

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    started.wait(timeout=10)

    sockets = server.servers[0].sockets if server.servers else []
    port = sockets[0].getsockname()[1] if sockets else None
    assert port, "Server failed to bind"

    yield f"http://127.0.0.1:{port}", collection

    server.should_exit = True
    thread.join(timeout=5)
    for p in patches:
        p.stop()


def test_status_bar_shows_ingested_count(server_with_collection, browser):
    """The status bar should reflect how many episodes are ingested."""
    base_url, collection = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function(
        "!document.getElementById('status-bar').textContent.includes('Loading')",
        timeout=5000,
    )
    status_text = page.text_content("#status-bar")
    page.close()

    assert "/5 episodes ingested" in status_text
    assert "excerpts" in status_text


def test_coverage_line_shows_no_episodes(server_with_collection, browser):
    """Before ingestion, coverage should say no episodes ingested."""
    base_url, collection = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function(
        "!document.getElementById('coverage').textContent.includes('Loading')",
        timeout=5000,
    )
    coverage = page.text_content("#coverage")
    page.close()

    assert "No episodes ingested" in coverage


def test_coverage_line_shows_range_after_ingestion(server_with_collection, browser):
    """After ingestion, coverage should show episode range and dates."""
    base_url, collection = server_with_collection

    _ingest_into_collection(collection, "guid-1", 1)
    _ingest_into_collection(collection, "guid-3", 3)

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function(
        "!document.getElementById('coverage').textContent.includes('Loading')",
        timeout=5000,
    )
    coverage = page.text_content("#coverage")
    page.close()

    assert "2 episodes ingested" in coverage
    # Format is "Ep 1–3" with en-dash
    assert "1\u20133" in coverage
