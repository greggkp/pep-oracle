"""Test that the web UI correctly reflects ChromaDB ingestion state.

Reproduces the bug where episodes ingested via the CLI don't show as
ingested in the web UI because the server's ChromaDB client returns
stale data.

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
from pep_oracle.store import add_chunks


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
    """Write chunks directly to a ChromaDB collection — simulates CLI ingestion."""
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
        patch("pep_oracle.server.get_ingested_guids", wraps=_real_get_ingested_guids(collection)),
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


def _real_get_ingested_guids(collection):
    """Return a callable that reads ingested GUIDs from the given collection."""
    from pep_oracle.store import get_ingested_guids as _orig

    def _wrapper(col):
        # Always read from our test collection
        return _orig(collection)

    return _wrapper


def _get_episode_markers(page):
    """Return dict of {episode_number: is_not_ingested} from the dropdown.

    Keys are ints; values are True if the episode is NOT ingested (has star).
    """
    raw = page.evaluate("""() => {
        const opts = document.querySelectorAll('#episode-filter option');
        const result = {};
        for (const opt of opts) {
            if (!opt.value) continue;
            result[opt.value] = opt.textContent.endsWith(' *');
        }
        return result;
    }""")
    # JS object keys are strings; convert to int
    return {int(k): v for k, v in raw.items()}


def _wait_for_episodes(page):
    page.wait_for_function(
        "document.querySelectorAll('#episode-filter option').length > 3",
        timeout=5000,
    )


def test_initially_all_episodes_not_ingested(server_with_collection, browser):
    """Before any ingestion, all episodes should show a star marker."""
    base_url, collection = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    _wait_for_episodes(page)
    markers = _get_episode_markers(page)
    page.close()

    assert len(markers) == 5, f"Expected 5 episodes in dropdown, got {len(markers)}"
    for ep_num in range(1, 6):
        assert markers[ep_num] is True, f"Episode {ep_num} should show as not ingested"


def test_ingested_episodes_shown_correctly(server_with_collection, browser):
    """After ingestion, episodes must show without the star marker."""
    base_url, collection = server_with_collection

    # Ingest episodes 1 and 3
    _ingest_into_collection(collection, "guid-1", 1)
    _ingest_into_collection(collection, "guid-3", 3)

    page = browser.new_page()
    page.goto(base_url)
    _wait_for_episodes(page)
    markers = _get_episode_markers(page)
    page.close()

    assert markers[1] is False, "Episode 1 should show as ingested"
    assert markers[3] is False, "Episode 3 should show as ingested"
    assert markers[2] is True, "Episode 2 should still show as not ingested"
    assert markers[4] is True, "Episode 4 should still show as not ingested"


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

    # Should contain "N/5 episodes ingested" where N reflects what's been ingested
    assert "/5 episodes ingested" in status_text
    assert "excerpts" in status_text


