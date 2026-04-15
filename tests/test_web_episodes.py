"""Test that the web UI correctly reflects ingestion status.

Uses Playwright against the real FastAPI app with an in-memory ChromaDB
collection and a mocked RSS feed.
"""

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
import uvicorn

pytest.importorskip("playwright.sync_api", reason="playwright not installed")

from pep_oracle.models import Chunk, Episode
from pep_oracle.store import add_chunks, get_ingested_guids, get_ingestion_stats
from pep_oracle.topics import extract_topics as _real_extract_topics


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
    client = chromadb.EphemeralClient()
    collection = client.get_or_create_collection(
        name="pep_oracle_" + uuid.uuid4().hex[:8], metadata={"hnsw:space": "cosine"}
    )

    patches = [
        patch("pep_oracle.server.fetch_episodes", return_value=EPISODES),
        patch("pep_oracle.server._get_fresh_collection", return_value=collection),
        patch("pep_oracle.server.get_ingested_guids", wraps=lambda col: get_ingested_guids(collection)),
        patch("pep_oracle.server.get_ingestion_stats", wraps=lambda col: get_ingestion_stats(collection)),
        patch("pep_oracle.server.CHROMA_DIR", Path("/tmp/fake-chroma")),
        patch("pep_oracle.server.extract_topics", return_value={
            "topics": [
                {"topic": "Topic from Ep 3", "question": "What about Ep 3?", "episode_number": 3},
                {"topic": "Topic from Ep 5", "question": "What about Ep 5?", "episode_number": 5},
            ],
            "pool": [
                {"topic": "Pool Topic A", "question": "What about Pool Topic A on the latest episode?", "episode_number": 4},
                {"topic": "Pool Topic B", "question": "What about Pool Topic B on the latest episode?", "episode_number": 3},
            ],
        }),
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
        "document.getElementById('status-bar').textContent.includes('episodes ingested')",
        timeout=15000,
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
        "document.getElementById('coverage').textContent.includes('No episodes')",
        timeout=15000,
    )
    coverage = page.text_content("#coverage")
    page.close()

    assert "No episodes ingested" in coverage


def test_coverage_line_shows_range_after_ingestion(server_with_collection, browser):
    """After ingestion, coverage should show episode range and dates."""
    base_url, collection = server_with_collection

    _ingest_into_collection(collection, "guid-1", 1)
    _ingest_into_collection(collection, "guid-3", 3)

    # Pre-populate status cache with fresh data so the page renders immediately
    from pep_oracle.server import _caches, _fetch_status
    _caches["status"].set(_fetch_status())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_function(
        "document.getElementById('coverage').textContent.includes('2 episodes')",
        timeout=15000,
    )
    coverage = page.text_content("#coverage")
    page.close()

    assert "2 episodes ingested" in coverage
    # Format is "Ep 1–3" with en-dash
    assert "1\u20133" in coverage


def test_not_ingested_chips_have_amber_styling(server_with_collection, browser):
    """Chips for un-ingested episodes should have the not-ingested CSS class."""
    base_url, collection = server_with_collection

    # Ingest episode 3 only — episode 5 remains un-ingested
    _ingest_into_collection(collection, "guid-3", 3)

    # Pre-populate topics cache with fresh data reflecting ingestion
    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=10000)

    chips = page.query_selector_all(".topic-chip")
    assert len(chips) >= 2

    for chip in chips:
        ep_num = chip.get_attribute("data-episode")
        if ep_num == "5":
            assert "not-ingested" in chip.get_attribute("class")
        elif ep_num == "3":
            assert "not-ingested" not in chip.get_attribute("class")

    page.close()


def test_ingest_banner_visible_when_not_ingested(server_with_collection, browser):
    """The ingest banner should appear when un-ingested episodes exist."""
    base_url, collection = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=10000)

    banner = page.query_selector("#ingest-banner")
    assert banner is not None
    # Banner should be visible (display: flex when episodes are not ingested)
    assert banner.is_visible()

    banner_text = banner.text_content()
    # All episodes 1-5 are not ingested, so banner should mention them
    assert "not yet ingested" in banner_text

    page.close()


def test_ingest_banner_hidden_when_all_ingested(server_with_collection, browser):
    """The ingest banner should not appear when all episodes are ingested."""
    base_url, collection = server_with_collection

    for i in range(1, 6):
        _ingest_into_collection(collection, f"guid-{i}", i)

    # Pre-populate topics cache with fresh data reflecting all ingested
    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=10000)

    banner = page.query_selector("#ingest-banner")
    assert banner is None or not banner.is_visible()

    page.close()


def test_not_ingested_chip_tooltip(server_with_collection, browser):
    """Un-ingested chips should show '(not yet ingested)' in their tooltip."""
    base_url, collection = server_with_collection

    # Ingest episode 3 only
    _ingest_into_collection(collection, "guid-3", 3)

    # Pre-populate topics cache with fresh data reflecting ingestion
    from pep_oracle.server import _caches, _fetch_topics
    _caches["topics"].set(_fetch_topics())

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=10000)

    chips = page.query_selector_all(".topic-chip")
    for chip in chips:
        ep_num = chip.get_attribute("data-episode")
        title = chip.get_attribute("title")
        if ep_num == "5":
            assert "(not yet ingested)" in title
        elif ep_num == "3":
            assert "(not yet ingested)" not in title

    page.close()


def test_chip_click_adds_used_class(server_with_collection, browser):
    """Clicking a topic chip adds the 'used' CSS class."""
    base_url, _ = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=15000)

    chip = page.locator(".topic-chip").first
    chip.click()

    assert "used" in chip.get_attribute("class")
    page.close()


def test_chip_click_appends_pool_chip(server_with_collection, browser):
    """Clicking a chip appends a new chip from the pool."""
    base_url, _ = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=15000)

    initial_count = page.locator(".topic-chip").count()
    page.locator(".topic-chip").first.click()

    new_count = page.locator(".topic-chip").count()
    assert new_count == initial_count + 1

    # The new chip should be the first pool topic
    last_chip = page.locator(".topic-chip").last
    assert last_chip.text_content() == "Pool Topic A"
    page.close()


def test_used_chip_still_populates_question(server_with_collection, browser):
    """Clicking a used chip still populates the question field without adding another pool chip."""
    base_url, _ = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=15000)

    chip = page.locator(".topic-chip").first
    chip.click()  # First click: marks as used, appends pool chip
    count_after_first = page.locator(".topic-chip").count()

    chip.click()  # Second click: should NOT append another pool chip
    count_after_second = page.locator(".topic-chip").count()
    assert count_after_second == count_after_first

    # Question field should still be populated
    question_val = page.locator("#question").input_value()
    assert len(question_val) > 0
    page.close()


def test_pool_exhaustion_no_extra_chips(server_with_collection, browser):
    """When pool is empty, clicking a chip adds no new chip."""
    base_url, _ = server_with_collection

    page = browser.new_page()
    page.goto(base_url)
    page.wait_for_selector(".topic-chip", timeout=15000)

    # Click all original chips to exhaust the pool (2 original chips, 2 pool entries)
    chips = page.locator(".topic-chip:not(.used)")
    chips.nth(0).click()  # uses pool entry 1
    chips = page.locator(".topic-chip:not(.used)")
    chips.nth(0).click()  # uses pool entry 2

    # Now click remaining unused chips — pool entries themselves
    chips = page.locator(".topic-chip:not(.used)")
    count_before = page.locator(".topic-chip").count()
    if chips.count() > 0:
        chips.nth(0).click()  # pool is exhausted, no new chip
    count_after = page.locator(".topic-chip").count()
    assert count_after == count_before
    page.close()
